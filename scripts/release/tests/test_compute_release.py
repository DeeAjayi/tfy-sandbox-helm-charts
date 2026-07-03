from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "compute_release.py"
)
SPEC = importlib.util.spec_from_file_location("compute_release", MODULE_PATH)
compute_release = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
# Register the module in sys.modules BEFORE exec_module — Python's @dataclass
# decorator does `sys.modules[cls.__module__].__dict__` lookup during class
# creation, which raises AttributeError on None when the module isn't yet
# registered. (Python 3.10 + dataclass + dynamic import quirk.)
sys.modules["compute_release"] = compute_release
SPEC.loader.exec_module(compute_release)


def test_compute_release_rc_first_iteration_defaults_to_all_repos(monkeypatch):
    monkeypatch.setattr(
        compute_release,
        "latest_shipped_version",
        lambda: compute_release.parse_semver("0.147.1"),
    )
    monkeypatch.setattr(compute_release, "all_repos", lambda: ["a/repo1", "a/repo2"])
    monkeypatch.setattr(compute_release, "next_rc_number", lambda *_: 1)

    spec = compute_release.compute_release("rc", [], "")

    # latest shipped 0.147.1 -> next minor 0.148.0 (continues the in-flight line)
    assert spec["target_version"] == "0.148.0"
    assert spec["tag"] == "v0.148.0-rc.1"
    assert spec["repos"] == ["a/repo1", "a/repo2"]
    assert spec["mode"] == "build"


def test_latest_service_line_tag_caps_at_base_patch(monkeypatch):
    # The service has shipped many patches on the 0.152 line. An older-base
    # hotfix (base 0.152.0) must pick v0.152.0 — NOT leap-frog onto the later
    # mainline v0.152.5 — while a hotfix at the line head (base 0.152.5) may use
    # the head. A higher in-progress RC (v0.152.6-rc.1) is excluded for base
    # 0.152.5 since it is past the base patch.
    refs = [
        {"ref": "refs/tags/v0.152.0"},
        {"ref": "refs/tags/v0.152.1"},
        {"ref": "refs/tags/v0.152.5"},
        {"ref": "refs/tags/v0.152.6-rc.1"},
        {"ref": "refs/tags/v0.153.0"},
    ]
    monkeypatch.setattr(compute_release, "gh_api", lambda *_a, **_k: refs)

    older = compute_release.parse_semver("0.152.0")
    assert str(compute_release.latest_service_line_tag("a/svc1", older)) == "0.152.0"

    head = compute_release.parse_semver("0.152.5")
    assert str(compute_release.latest_service_line_tag("a/svc1", head)) == "0.152.5"

    # No tag at-or-below the base patch -> None (falls back to the chart pin).
    monkeypatch.setattr(
        compute_release,
        "gh_api",
        lambda *_a, **_k: [{"ref": "refs/tags/v0.152.5"}],
    )
    assert compute_release.latest_service_line_tag("a/svc1", older) is None


def _stub_hotfix_env(monkeypatch, current_tags, service_tags=None):
    monkeypatch.setattr(
        compute_release,
        "latest_shipped_version",
        lambda: compute_release.parse_semver("0.1.4"),
    )
    monkeypatch.setattr(compute_release, "all_repos", lambda: list(current_tags))
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: True)
    monkeypatch.setattr(compute_release, "next_rc_number", lambda *_: 1)
    monkeypatch.setattr(
        compute_release, "find_component", lambda repo: {"repository": repo},
    )
    monkeypatch.setattr(
        compute_release,
        "read_component_image_tag",
        lambda component, _ref: current_tags[component["repository"]],
    )
    # The service repo's own latest tag on the base's minor line. Defaults to
    # None (no tag found -> fall back to the chart pin in current_tags), which
    # preserves the chart-pin behavior the older tests assert.
    service_tags = service_tags or {}
    monkeypatch.setattr(
        compute_release,
        "latest_service_line_tag",
        lambda repo, _base: (
            compute_release.parse_image_tag(service_tags[repo])
            if service_tags.get(repo)
            else None
        ),
    )


def test_hotfix_tags_are_sequential_per_repo(monkeypatch):
    # Chart line is at 0.1.4 (-> 0.1.5), but each service advances its OWN patch.
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0", "a/svc2": "v0.2.3"})

    spec = compute_release.compute_release("hotfix", ["a/svc1", "a/svc2"], "")

    assert spec["chart_version"] == "0.1.5-rc.1"
    assert spec["repo_tags"] == {
        "a/svc1": "v0.1.1-rc.1",
        "a/svc2": "v0.2.4-rc.1",
    }
    # Each service branch is cut from its OWN current shipped tag, decoupled
    # from the chart line (0.1.4) — NOT a chart-aligned v0.1.4 for every repo.
    assert spec["branch_source_refs"] == {
        "a/svc1": "refs/tags/v0.1.0",
        "a/svc2": "refs/tags/v0.2.3",
    }
    # Each service BUILDS on a branch named after its OWN target patch, NOT the
    # chart branch (release-v0.1.5).
    assert spec["branch"] == "release-v0.1.5"
    assert spec["repo_branches"] == {
        "a/svc1": "release-v0.1.1",
        "a/svc2": "release-v0.2.4",
    }


def test_hotfix_advances_from_service_latest_when_chart_pin_is_stale(monkeypatch):
    # Repro of the reported bug: the chart pins svc1 at v0.1.0, but the service
    # already shipped v0.1.3 on this line. The per-repo target must advance from
    # the service's OWN latest (v0.1.3 -> v0.1.4) rather than the stale chart pin
    # (which would recompute the already-shipped v0.1.1 and skip the build), and
    # the service branch must be cut from v0.1.3.
    _stub_hotfix_env(
        monkeypatch,
        {"a/svc1": "v0.1.0"},
        service_tags={"a/svc1": "v0.1.3"},
    )

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    assert spec["repo_tags"] == {"a/svc1": "v0.1.4-rc.1"}
    assert spec["branch_source_refs"] == {"a/svc1": "refs/tags/v0.1.3"}
    # Chart line is 0.1.4 -> 0.1.5, but svc1 builds on its OWN release-v0.1.4.
    assert spec["branch"] == "release-v0.1.5"
    assert spec["repo_branches"] == {"a/svc1": "release-v0.1.4"}


def test_hotfix_keeps_chart_pin_when_service_tag_is_older(monkeypatch):
    # Reconciliation never moves a service backward: when the service's own
    # latest line tag (v0.1.0) is older than the chart pin (v0.2.3), the chart
    # pin wins and the patch advances from it.
    _stub_hotfix_env(
        monkeypatch,
        {"a/svc1": "v0.2.3"},
        service_tags={"a/svc1": "v0.1.0"},
    )

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    assert spec["repo_tags"] == {"a/svc1": "v0.2.4-rc.1"}
    assert spec["branch_source_refs"] == {"a/svc1": "refs/tags/v0.2.3"}


def test_hotfix_service_builds_on_own_branch_when_chart_line_is_ahead(monkeypatch):
    # Chart line is well ahead of the service: chart 0.152.5 -> 0.152.6, while
    # servicefoundry-server's own latest tag is only v0.152.0 -> v0.152.1. The
    # chart PR uses release-v0.152.6, but the service must BUILD on its own
    # release-v0.152.1 (the operator-created branch carrying the fix), cut from
    # v0.152.0. This is the divergent case the per-repo branch decoupling fixes.
    _stub_hotfix_env(
        monkeypatch,
        {"a/svc1": "v0.152.0"},
        service_tags={"a/svc1": "v0.152.0"},
    )
    monkeypatch.setattr(
        compute_release,
        "latest_shipped_version",
        lambda: compute_release.parse_semver("0.152.5"),
    )
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: False)

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    # Chart bumps its OWN line and references the new service tag.
    assert spec["branch"] == "release-v0.152.6"
    assert spec["chart_version"] == "0.152.6-rc.1"
    # Service advances its OWN line and builds on its OWN branch.
    assert spec["repo_tags"] == {"a/svc1": "v0.152.1-rc.1"}
    assert spec["repo_branches"] == {"a/svc1": "release-v0.152.1"}
    assert spec["branch_source_refs"] == {"a/svc1": "refs/tags/v0.152.0"}


def test_hotfix_same_patch_keeps_service_branch_equal_to_chart_branch(monkeypatch):
    # Common case: chart and service are on the same patch line. The per-repo
    # build branch equals the chart branch, so nothing diverges.
    _stub_hotfix_env(
        monkeypatch,
        {"a/svc1": "v0.1.4"},
        service_tags={"a/svc1": "v0.1.4"},
    )
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: False)

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    assert spec["branch"] == "release-v0.1.5"
    assert spec["repo_tags"] == {"a/svc1": "v0.1.5-rc.1"}
    assert spec["repo_branches"] == {"a/svc1": "release-v0.1.5"}


def test_hotfix_skip_rc_builds_final_per_repo_patch(monkeypatch):
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})

    spec = compute_release.compute_release(
        "hotfix", ["a/svc1"], "", skip_rc=True,
    )

    assert spec["chart_version"] == "0.1.5"
    assert spec["repo_tags"] == {"a/svc1": "v0.1.1"}
    assert spec["branch_source_refs"] == {"a/svc1": "refs/tags/v0.1.0"}


def test_hotfix_holds_patch_during_rc_cycle(monkeypatch):
    # A repo already mid-cycle (current tag is an -rc) keeps its patch; only the
    # shared RC counter advances.
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.1-rc.1"})
    monkeypatch.setattr(compute_release, "next_rc_number", lambda *_: 2)

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    assert spec["repo_tags"] == {"a/svc1": "v0.1.1-rc.2"}


def test_hotfix_branch_is_per_target_patch(monkeypatch):
    # base 0.1.4 -> target 0.1.5 -> per-patch branch release-v0.1.5 (named after
    # the TARGET, NOT the per-minor release-v0.1.0). base 0.1.4 == latest shipped,
    # so this is a latest-line hotfix and the CHART branch is cut from main
    # (which is fast-forwarded to 0.1.4's chart files on every latest-line prod).
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: False)

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "0.1.4")

    assert spec["branch"] == "release-v0.1.5"
    assert spec["chart_branch_source_ref"] == "refs/heads/main"
    assert spec["branch_source_ref"] == "refs/tags/v0.1.4"
    # The bug repro: chart base is 0.1.4 but svc1 only shipped v0.1.0, so the
    # service branch must be cut from v0.1.0 (its own tag), not v0.1.4.
    assert spec["branch_source_refs"] == {"a/svc1": "refs/tags/v0.1.0"}
    assert spec["target_version"] == "0.1.5"


def test_hotfix_defaults_base_to_latest_shipped(monkeypatch):
    # No base_version -> base is the latest shipped final (0.1.4 in the stub),
    # so target 0.1.5 and the branch is release-v0.1.5. This is on the latest
    # line, so the chart branch is cut from main (fast-forwarded to 0.1.4).
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: False)

    spec = compute_release.compute_release("hotfix", ["a/svc1"], "")

    assert spec["branch"] == "release-v0.1.5"
    assert spec["chart_branch_source_ref"] == "refs/heads/main"


def test_hotfix_on_latest_line_marks_deploy_gate(monkeypatch):
    # Default base = latest shipped (0.1.4) -> target 0.1.5 is at/ahead of the
    # latest line, so on_latest_line is True for BOTH the RC and the final, and
    # fast_forward_main (final-only "advance main") is set only for skip_rc.
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})

    rc_spec = compute_release.compute_release("hotfix", ["a/svc1"], "")
    assert rc_spec["on_latest_line"] is True
    assert rc_spec["fast_forward_main"] is False

    final_spec = compute_release.compute_release(
        "hotfix", ["a/svc1"], "", skip_rc=True,
    )
    assert final_spec["on_latest_line"] is True
    assert final_spec["fast_forward_main"] is True


def test_hotfix_on_older_line_is_not_on_latest_line(monkeypatch):
    # latest shipped is 0.1.4; hotfixing the older 0.1.2 line -> target 0.1.3 is
    # behind the latest line, so it must NOT deploy to develop/prod (those track
    # the latest line). Both gates stay False even for a skip_rc final.
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})

    rc_spec = compute_release.compute_release("hotfix", ["a/svc1"], "0.1.2")
    assert rc_spec["on_latest_line"] is False
    # Older line never promotes to main, so its chart branch is cut from the
    # truefoundry-<base> tag, NOT main.
    assert rc_spec["chart_branch_source_ref"] == "refs/tags/truefoundry-0.1.2"

    final_spec = compute_release.compute_release(
        "hotfix", ["a/svc1"], "0.1.2", skip_rc=True,
    )
    assert final_spec["on_latest_line"] is False
    assert final_spec["fast_forward_main"] is False
    assert final_spec["chart_branch_source_ref"] == "refs/tags/truefoundry-0.1.2"


def test_hotfix_chart_only_requires_empty_repositories(monkeypatch):
    _stub_hotfix_env(monkeypatch, {"a/svc1": "v0.1.0"})

    with pytest.raises(ValueError, match="chart_only hotfix must not specify repositories"):
        compute_release.compute_release("hotfix", ["a/svc1"], "", chart_only=True)


def test_hotfix_chart_only_builds_no_services(monkeypatch):
    # Chart-only hotfix: template/version bump with no service touched. The
    # chart branch/version math still runs exactly like a normal hotfix; only
    # the per-repo plan collapses to empty.
    _stub_hotfix_env(monkeypatch, {})
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: False)

    spec = compute_release.compute_release("hotfix", [], "", chart_only=True)

    assert spec["repos"] == []
    assert spec["repo_tags"] == {}
    assert spec["branch_source_refs"] == {}
    assert spec["repo_branches"] == {}
    assert spec["mode"] == "build"
    assert spec["chart_only"] is True
    # Default base = latest shipped (0.1.4) -> target 0.1.5, on the latest
    # line, so the chart branch is cut from main (same rule as any other
    # latest-line hotfix).
    assert spec["branch"] == "release-v0.1.5"
    assert spec["chart_branch_source_ref"] == "refs/heads/main"


def test_chart_only_rejected_for_non_hotfix_kinds(monkeypatch):
    monkeypatch.setattr(
        compute_release, "latest_shipped_version",
        lambda: compute_release.parse_semver("0.147.1"),
    )
    monkeypatch.setattr(compute_release, "all_repos", lambda: [])

    with pytest.raises(ValueError, match="chart_only is only supported for kind=hotfix"):
        compute_release.compute_release("rc", [], "", chart_only=True)


def test_prod_promotes_rc_from_per_patch_hotfix_branch(monkeypatch):
    # A hotfix RC 0.147.2-rc.1 lives on release-v0.147.2. Promoting it with
    # base_version=0.147.2 must resolve THAT branch (not release-v0.147.0).
    monkeypatch.setattr(
        compute_release,
        "latest_shipped_version",
        lambda: compute_release.parse_semver("0.148.4"),
    )
    monkeypatch.setattr(
        compute_release, "read_yaml_at_ref", lambda *_: {"version": "0.147.2-rc.1"},
    )
    monkeypatch.setattr(compute_release, "all_repos", lambda: ["a/repo1"])
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: True)

    spec = compute_release.compute_release("prod", [], "0.147.2")

    assert spec["branch"] == "release-v0.147.2"
    assert spec["target_version"] == "0.147.2"
    assert spec["source_rc_tag"] == "v0.147.2-rc.1"
    # older line -> must NOT fast-forward main
    assert spec["fast_forward_main"] is False


def test_compute_release_prod_requires_rc_on_branch(monkeypatch):
    monkeypatch.setattr(
        compute_release,
        "latest_shipped_version",
        lambda: compute_release.parse_semver("0.147.1"),
    )
    monkeypatch.setattr(
        compute_release, "read_yaml_at_ref", lambda *_: {"version": "0.148.0"},
    )
    monkeypatch.setattr(compute_release, "all_repos", lambda: ["a/repo1"])
    monkeypatch.setattr(compute_release, "git_branch_exists", lambda *_: True)

    try:
        compute_release.compute_release("prod", [], "")
        assert False, "expected ValueError"
    except ValueError as error:
        assert "not at an RC" in str(error)
