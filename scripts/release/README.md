# Release Orchestrator

A single GitHub Actions pipeline in `helm-charts` that drives a release from
"operator clicks Run" to "chart published + git tags/releases created". It builds
service images (by dispatching each repo's own `build-n-publish-release*`
workflow), updates the umbrella chart, and publishes — for `rc`, `prod`, and
`hotfix` kinds.

## Entry point

`.github/workflows/release-start.yml` (`workflow_dispatch`). Inputs:

| Input | Required | Notes |
|---|---|---|
| `kind` | yes | `rc` \| `prod` \| `hotfix` |
| `repositories` | no | JSON array. Empty = compute default (first `rc`/`prod` = all repos). Required for continuing `rc` and `hotfix`. |
| `base_version` | no | Line to act on, e.g. `0.140.5`. Optional for `hotfix` (default = latest shipped) and `prod` (default = next minor's RC). Ignored for `rc`. |
| `skip_rc` | no | Hotfix only: build the final patch directly with no RC stage. |
| `chart_only` | no | Hotfix only: bump ONLY the umbrella chart (templates/version) — no service is built or retagged. `repositories` must be empty. For template-only fixes to `charts/truefoundry` or `charts/tfy-llm-gateway` that don't touch any service image. |
| `merge_pr` | no | Merge the chart PR + continue to publish. Disable to stop after opening the PR. |
| `allow_admin_merge` | no | Break-glass: bypass branch protection with an admin merge if a normal merge is blocked. |
| `sync_main` | no | Merge `main` into the release branch before updating (latest line only). |

## Release scenarios

Trigger via `gh workflow run release-start.yml --repo truefoundry/helm-charts -f ...`
(or the Actions UI). Assume the latest shipped prod tag is `truefoundry-0.147.1`.

### Weekly RC (start the next minor)
First RC of the next minor line; empty `repositories` builds all components.
```
gh workflow run release-start.yml -f kind=rc -f repositories='[]'
```
→ `v0.148.0-rc.1`, cuts `release-v0.148.0`, builds all repos, merges chart PR,
publishes `truefoundry-0.148.0-rc.1`. `main` untouched.

### Continue an RC (cherry-pick a few repos)
`repositories` is required for a continuing RC.
```
gh workflow run release-start.yml -f kind=rc \
  -f repositories='["truefoundry/servicefoundry-server"]'
```
→ `v0.148.0-rc.2` on `release-v0.148.0`; only the listed repos rebuild, others keep
their existing tag.

### Promote the latest line to prod
Reads the in-flight RC on the next-minor branch, strips `-rc`, retags, fast-forwards `main`.
```
gh workflow run release-start.yml -f kind=prod -f repositories='[]'
```
→ `v0.148.0`, retags all components, publishes `truefoundry-0.148.0`, `main` → `0.148.0`.

### Hotfix the latest shipped line (RC then promote)
```
gh workflow run release-start.yml -f kind=hotfix \
  -f repositories='["truefoundry/servicefoundry-server"]'
gh workflow run release-start.yml -f kind=prod -f base_version=0.147.2
```
→ builds `v0.147.2-rc.1` on `release-v0.147.2` (deploys the RC to **develop**
for QA), then promotes to `v0.147.2` (latest line → `main` advances, deploys to
**develop + prod**). Note `base_version` for the `prod` step is the HOTFIX'S
OWN target branch name (`release-v<target>`), not the line it started from.

### Chart-only hotfix (template/version bump, no service rebuild)
For a fix that only touches `charts/truefoundry` or `charts/tfy-llm-gateway`
templates/config — no service image needs rebuilding. `repositories` must be
empty; `compute_release.py` rejects `chart_only=true` with any repos listed.
```
gh workflow run release-start.yml -f kind=hotfix \
  -f repositories='[]' -f chart_only=true
```
→ builds `v0.147.2-rc.1` on `release-v0.147.2` exactly like a normal hotfix
(same branch-naming, same latest/older-line source-ref rule), except the
`build`/`retag` matrix has zero legs — nothing is built or retagged, only the
chart's own version/templates move. Combine with `skip_rc=true` to ship the
template fix directly as a final patch with no RC stage. Promote to prod the
same way as any other hotfix (`kind=prod -f base_version=<target>`).

### Hotfix directly to final (no RC stage)
```
gh workflow run release-start.yml -f kind=hotfix \
  -f repositories='["truefoundry/servicefoundry-server"]' -f skip_rc=true
```
→ builds `v0.147.2` directly; advances `main` since it is the latest line, and
deploys to **develop + prod**.

### Hotfix an OLDER line (e.g. customer on 0.140.x while latest is 0.148)
`base_version` is required to target the old line; its `release-v0.140.0` branch
must already exist with the fix cherry-picked in.
```
gh workflow run release-start.yml -f kind=hotfix \
  -f repositories='["truefoundry/servicefoundry-server"]' -f base_version=0.140.5
gh workflow run release-start.yml -f kind=prod -f base_version=0.140.6
```
→ `v0.140.6-rc.1` then `v0.140.6`, both on `release-v0.140.6` (cut from the
`truefoundry-0.140.5` tag, not `main`). `main` untouched (`0.140.6 < 0.148`);
the chart is published for the old line. The develop/prod clusters are
**not** touched (they track the latest line) — an older-line fix is rolled
out to its own customer cluster separately.

### Inspect only (don't merge)
```
gh workflow run release-start.yml -f kind=rc -f merge_pr=false
```
→ builds + opens the chart PR, then stops (no merge, no publish).

### Pull main into the current line before releasing
```
gh workflow run release-start.yml -f kind=rc -f sync_main=true
```
→ merges `main` into `release-v0.148.0` (latest line only) before updating values.

### Optional flags
- `merge_pr=false` — stop after opening the chart PR.
- `allow_admin_merge=true` — bypass branch protection if a normal merge is blocked.
- `sync_main=true` — merge `main` into the release branch (latest line only).
- `skip_rc=true` — hotfix only; build the final patch without an RC stage.
- `chart_only=true` — hotfix only; bump only the chart (no service build/retag); `repositories` must be empty.

## Source of truth

- **Latest shipped prod**: the highest final chart git tag `truefoundry-X.Y.Z`
  (no `-rc`). `compute_release.py` reads this — not `main`'s `Chart.yaml`.
- **`release-vX.Y.0`** (per minor line): holds the line's in-flight `Chart.yaml`
  version and `values.yaml` image tags. RCs, prod, and hotfixes all merge here.
- **`main`**: a deploy pointer, fast-forwarded only on latest-line prod
  (`target >= latest shipped`). The release flow never reads `main` for version math.
  Promotion into `main` is a **squash** (merge commits are disallowed), so the
  real release-branch commits never land on `main`. A latest-line hotfix
  therefore cuts its CHART branch from `main` (`chart_branch_source_ref =
  refs/heads/main`), not the `truefoundry-<base>` tag (which sits on the
  un-squashed release lineage): cutting from the tag makes the merge-base with
  `main` fall far back, so the promote PR re-lists the whole line instead of just
  the hotfix commit. Older-line hotfixes don't promote to `main` and are cut from
  the `truefoundry-<base>` tag.
- **Container registry**: whether `repo:tag` is built.
- **Service-repo git tags**: `vX.Y.Z[-rc.N]`. Chart git tags: `truefoundry-X.Y.Z[-rc.N]`.

## Versioning model

The umbrella chart version and the per-service image tags are **two independent
version namespaces**. The chart version (`Chart.yaml.version`, mirrored into
`global.controlPlaneChartVersion`) is a single release-train / bill-of-materials
number; each service is pinned **independently** in `values.yaml`.

- A service advances its **own** patch from its **own** current tag
  (`0.150.0 -> 0.150.1`), regardless of where the chart line sits
  (`0.150.1 -> 0.150.2`). It is `bump_patch` relative to the service's own tag —
  it never jumps to match the chart's patch number. See `hotfix_repo_plan` in
  `compute_release.py`.
- A service that isn't part of a release keeps its existing tag untouched.
- Therefore **one chart release legitimately pins varying service tag
  versions**. `update_values.py` writes each service's tag from the per-repo
  `tag_map` and the single `chart_version` separately, so they never move in
  lockstep. The chart/helm-update PR body lists the full per-service tag map for
  exactly this reason.

Worked example — hotfix on the `0.150.x` line where the chart led the services:

| What | Before | After | Notes |
|---|---|---|---|
| Umbrella chart | `0.150.1` | `0.150.2` | single release-train number |
| `servicefoundry-server` | `0.150.1` | `0.150.2` | touched: its own patch +1 |
| `tfy-k8s-controller` | `0.150.0` | `0.150.0` | untouched: keeps its tag |

This is the standard umbrella-chart pattern: the chart version answers "which
tested bundle is this", not "what version is every service". Branch source refs
follow the same per-service rule — a missing service release branch is cut from
that repo's **own** current tag via `branch_source_refs` (not a
chart-version-aligned tag, which may not exist in the service repo).

A service's **current** tag for a hotfix is the newer of the chart's pinned tag
and the service repo's **own** latest `vX.Y.*` tag on that minor line, bounded to
`patch <= base.patch` (`latest_service_line_tag` in `compute_release.py`). This
matters when the chart pin lags what the service actually shipped (chart still
pins `v0.152.0` while the service already shipped `v0.152.1`): without it the
hotfix would recompute the already-shipped patch and the build would be skipped.
The `patch <= base.patch` bound keeps an older-base hotfix (`base_version` on a
minor mainline has since advanced) from leap-frogging onto a later mainline tag
— a fix off base `0.152.0` yields `v0.152.1`, never `v0.152.6` just because
`v0.152.5` shipped later on the same minor.

Each service also **builds on its own branch** named after its service target —
`spec.repo_branches[repo]` = `release-v<service_target>` — which can differ from
the chart PR branch (`spec.branch` = `release-v<chart_target>`) when the chart
line runs ahead. Example: chart `0.152.5 -> 0.152.6` while `servicefoundry-server`
goes `0.152.0 -> 0.152.1`; the chart PR lands on `release-v0.152.6` but the
service is built on the operator-created `release-v0.152.1` (cut from `v0.152.0`)
and tagged `v0.152.1`. `ensure_service_branches.py` and `dispatch_build.py` use
`repo_branches[repo]`, falling back to `spec.branch` for rc/prod and same-patch
hotfixes.

## Flow

1. `compute_release` → emits the `spec` (target_version, chart_version, tag,
   branch, repos, mode, fast_forward_main, is_latest_line; hotfix also sets
   `on_latest_line` — the cluster-deploy gate, see step 8 — plus per-repo
   `repo_tags`, `branch_source_refs`, and `repo_branches`).
2. Build path (`mode=build`): `ensure_service_branches` then
   `release-build-services.yml` matrix — each leg dispatches the repo's
   `build-n-publish-release*` workflow and polls it. Per-repo outcome:
   `built` (rebuilt), `unchanged` (commit already built → kept existing tag), or
   `already_tagged` (release tag already present).
3. Retag path (`mode=retag`, prod): `retag.py` reads each repo's currently pinned
   tag from `values.yaml` on the release branch and copies that image to the final
   `vX.Y.Z` (handles mixed RCs per repo).
4. `build_summary.py` aggregates per-repo results into `tag_map` (values updates)
   and `commit_map` (git tag/release targets); fails if any expected repo is missing.
5. `release-helm-update.yml`: updates `values.yaml` + `Chart.yaml` on
   `release-vX.Y.0`, opens a PR, merges it (and fast-forwards `main` when eligible).
6. `oci-release.yml` (existing) publishes the chart to JFrog on the branch push.
7. `release-publish-releases.yml`: creates git tags + GitHub Releases per built
   service repo and the chart (`truefoundry-X.Y.Z`); prerelease for `rc`/`hotfix`.
8. Cluster deploys (`deploy.py`, bumps `truefoundry`'s `targetRevision`):
   - `deploy_develop` → `ubermold-base` develop branch. Runs for `rc`, `prod`,
     and a **latest-line** `hotfix` (both its RC and its final).
   - `deploy_prod` → `ubermold-truefoundry` via an auto-merged PR. Runs for `prod`
     and a **latest-line** `hotfix` **final** (skip_rc → real `vX.Y.Z`).
   - Both clusters track the latest line, so older-line hotfixes are skipped
     (`spec.on_latest_line` / `fast_forward_main` gate this) and hotfix RCs never
     reach prod. The normal RC→promote flow still reaches prod via `kind=prod`.

## Scripts

- `_lib.py` — shared `run`/`gh_api`/component helpers.
- `compute_release.py` — version/branch/mode decision engine (tag-based source).
- `ensure_service_branches.py` — create each repo's release branch
  (`repo_branches[repo]`, else `spec.branch`) in service repos.
- `dispatch_build.py` — dispatch + poll a repo's build workflow on its own
  branch (`repo_branches[repo]`, else `spec.branch`); skip/reuse logic.
- `build_summary.py` — aggregate `tag_map`/`commit_map`; completeness check.
- `retag.py` — per-repo prod retag from `values.yaml`.
- `update_values.py` — write `values.yaml` image tags + `Chart.yaml.version`.
- `publish_releases.py` — git tags + GitHub Releases (idempotent).
- `summarize_release.py` — run record + step summary + optional Slack.
- `wait_for_merge.py` — poll the chart PR to merged state.

## Config

`config/components.yml` — per repo: `repository`, `helm_subpath` (the
`values.yaml` key(s)), `build.workflow_files` (the `build-n-publish-release*`
file(s) to dispatch), image `name`(s), and `registry` key
(`artifactory_private`/`artifactory_public`). Registry keys resolve via the
`ARTIFACTORY_*_REGISTRY` env (set from repo variables
`TRUEFOUNDRY_ARTIFACTORY_*_REPOSITORY`).

## Prerequisites / rollout notes

- Each service repo must expose its `build-n-publish-release*` workflow (with
  `workflow_dispatch` + `image_tag`) on its **default branch** to be dispatchable.
- `tfy-llm-gateway` / `tfy-otel-collector` are chart **dependencies**, not
  `values.yaml` image-tag subpaths — keep them out of a release's `repositories`
  until dependency-version handling is added.
- `release-v*` branch protection should require checks but not human approval (or
  allowlist the CI bot); otherwise use `allow_admin_merge`.

## Idempotency / safety

- Re-running the same release converges: built images are reused via registry
  manifest checks; unchanged repos keep their existing tag; the chart PR is a
  no-op when content matches; git tags fail loud on a SHA mismatch.
- A failed build leg blocks the Helm and publish stages.
- Latest-line prod promotions fast-forward `main`; older-line promotions do not.
