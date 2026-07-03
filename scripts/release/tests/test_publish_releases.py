from __future__ import annotations

import publish_releases


def test_resolve_previous_tag_uses_base_when_distinct(monkeypatch) -> None:
    monkeypatch.setattr(
        publish_releases,
        "tag_ref_sha",
        lambda repo, tag: "abc" if tag == "v0.147.1" else None,
    )
    spec = {"kind": "prod", "base": "0.147.1", "target_version": "0.148.0"}
    assert publish_releases.resolve_previous_tag(spec, "org/repo", "v0.148.0") == "v0.147.1"


def test_resolve_previous_tag_ignores_release_tag_after_ensure(monkeypatch) -> None:
    """When base equals the release tag, never diff a tag against itself."""
    monkeypatch.setattr(
        publish_releases,
        "tag_ref_sha",
        lambda repo, tag: "sha" if tag in {"v0.147.2", "v0.147.1"} else None,
    )
    spec = {"kind": "prod", "base": "0.147.2", "target_version": "0.147.2"}
    assert publish_releases.resolve_previous_tag(spec, "org/repo", "v0.147.2") == "v0.147.1"


def test_resolve_previous_tag_skips_self_even_if_only_candidate_exists(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        publish_releases,
        "tag_ref_sha",
        lambda repo, tag: "sha" if tag == "v0.147.2" else None,
    )
    spec = {"kind": "prod", "base": "0.147.2", "target_version": "0.147.2"}
    assert publish_releases.resolve_previous_tag(spec, "org/repo", "v0.147.2") is None


def test_prior_patch_tag() -> None:
    assert publish_releases._prior_patch_tag("0.147.2") == "v0.147.1"
    assert publish_releases._prior_patch_tag("0.147.0") is None
