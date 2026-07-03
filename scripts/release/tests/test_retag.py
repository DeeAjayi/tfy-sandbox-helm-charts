from __future__ import annotations

import importlib.util
import json
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "retag.py"
)
SPEC = importlib.util.spec_from_file_location("retag", MODULE_PATH)
retag = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(retag)


def test_read_source_tags_supports_mixed_rc_versions(monkeypatch):
    values_yaml = """
servicefoundryServer:
  image:
    tag: v0.149.0-rc.1
mlFoundryServer:
  image:
    tag: v0.149.0-rc.3
deltaFusionIngestor:
  image:
    tag: v0.149.0-rc.2
deltaFusionCompaction:
  image:
    tag: v0.149.0-rc.2
"""

    monkeypatch.setattr(retag, "git_show", lambda *_: values_yaml)
    monkeypatch.setattr(
        retag,
        "load_components",
        lambda: [
            {
                "repository": "truefoundry/servicefoundry-server",
                "helm_subpath": "servicefoundryServer",
            },
            {
                "repository": "truefoundry/mlfoundry-server",
                "helm_subpath": "mlFoundryServer",
            },
            {
                "repository": "truefoundry/deltafusion-ingestor",
                "helm_subpath": ["deltaFusionIngestor", "deltaFusionCompaction"],
            },
        ],
    )

    source = retag.read_source_tags(
        "release-v0.149.0",
        [
            "truefoundry/servicefoundry-server",
            "truefoundry/mlfoundry-server",
            "truefoundry/deltafusion-ingestor",
        ],
    )

    assert source["truefoundry/servicefoundry-server"] == "v0.149.0-rc.1"
    assert source["truefoundry/mlfoundry-server"] == "v0.149.0-rc.3"
    assert source["truefoundry/deltafusion-ingestor"] == "v0.149.0-rc.2"


def test_is_rc_source_tag_distinguishes_changed_from_unchanged():
    # Built this cycle -> carries an -rc tag -> should be retagged.
    assert retag.is_rc_source_tag("v0.149.0-rc.1") is True
    assert retag.is_rc_source_tag("0.149.0-rc.12") is True
    # Unchanged component still pinned to a prior FINAL tag -> must be skipped.
    assert retag.is_rc_source_tag("v0.140.0") is False
    assert retag.is_rc_source_tag("v0.149.0") is False


def test_image_names_returns_tag_suffixes():
    plain = {
        "repository": "truefoundry/servicefoundry-server",
        "build": {"image_artifact_name": "servicefoundry-server", "registry": "artifactory_private"},
    }
    suffixed = {
        "repository": "truefoundry/deltafusion-ingestor",
        "build": {
            "image_artifact_name": "deltafusion-ingestor",
            "registry": "artifactory_private",
            "tag_suffixes": ["-optimized"],
        },
    }

    assert retag.image_names(plain) == [
        ("servicefoundry-server", "artifactory_private", []),
    ]
    assert retag.image_names(suffixed) == [
        ("deltafusion-ingestor", "artifactory_private", ["-optimized"]),
    ]


def test_image_names_per_image_suffix_overrides_build():
    component = {
        "repository": "truefoundry/multi",
        "build": {
            "registry": "artifactory_private",
            "tag_suffixes": ["-optimized"],
            "images": [
                {"name": "image-a"},
                {"name": "image-b", "tag_suffixes": []},
                {"name": "image-c", "tag_suffixes": ["-slim", "-optimized"]},
            ],
        },
    }

    assert retag.image_names(component) == [
        ("image-a", "artifactory_private", ["-optimized"]),
        ("image-b", "artifactory_private", []),
        ("image-c", "artifactory_private", ["-slim", "-optimized"]),
    ]


def test_main_retags_optimized_variant_alongside_base(monkeypatch):
    values_yaml = """
servicefoundryServer:
  image:
    tag: v0.152.0-rc.5
deltaFusionIngestor:
  image:
    tag: v0.152.0-rc.5
deltaFusionCompaction:
  image:
    tag: v0.152.0-rc.5
"""

    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setattr(retag, "git_show", lambda *_: values_yaml)
    monkeypatch.setattr(
        retag,
        "load_components",
        lambda: [
            {
                "repository": "truefoundry/servicefoundry-server",
                "helm_subpath": "servicefoundryServer",
                "build": {
                    "image_artifact_name": "servicefoundry-server",
                    "registry": "artifactory_private",
                },
            },
            {
                "repository": "truefoundry/deltafusion-ingestor",
                "helm_subpath": ["deltaFusionIngestor", "deltaFusionCompaction"],
                "build": {
                    "image_artifact_name": "deltafusion-ingestor",
                    "registry": "artifactory_private",
                    "tag_suffixes": ["-optimized"],
                },
            },
        ],
    )
    monkeypatch.setattr(retag, "registry_prefix", lambda key: "registry.example/images")
    monkeypatch.setattr(retag, "registry_login", lambda *_: None)
    monkeypatch.setattr(retag, "resolve_tag_sha", lambda *_: "deadbeef")

    copies: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        retag,
        "copy_tag",
        lambda image, source_tag, target_tag: copies.append((image, source_tag, target_tag)),
    )

    spec = json.dumps(
        {
            "repos": [
                "truefoundry/servicefoundry-server",
                "truefoundry/deltafusion-ingestor",
            ],
            "branch": "release-v0.152.0",
            "tag": "v0.152.0",
        },
    )
    monkeypatch.setattr("sys.argv", ["retag.py", "--spec", spec])

    assert retag.main() == 0

    # Base tag promoted for both repos; the optimized variant is promoted only
    # for the component that publishes one.
    assert ("registry.example/images/servicefoundry-server", "v0.152.0-rc.5", "v0.152.0") in copies
    assert ("registry.example/images/deltafusion-ingestor", "v0.152.0-rc.5", "v0.152.0") in copies
    assert (
        "registry.example/images/deltafusion-ingestor",
        "v0.152.0-rc.5-optimized",
        "v0.152.0-optimized",
    ) in copies
    assert not any("-optimized" in source for image, source, _ in copies if "servicefoundry" in image)
