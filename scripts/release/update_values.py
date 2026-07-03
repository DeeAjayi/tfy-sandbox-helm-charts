#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


COMPONENTS_PATH = Path("config/components.yml")
UMBRELLA_VALUES = "charts/truefoundry/values.yaml"
UMBRELLA_CHART = "charts/truefoundry/Chart.yaml"


def load_components(components_path: Path) -> list[dict[str, Any]]:
    yaml = YAML(typ="safe")
    return (yaml.load(components_path.read_text(encoding="utf-8")) or {}).get("repositories", [])


def walk_to_map(root: dict[str, Any], dotted_path: str) -> dict[str, Any]:
    current: Any = root
    for segment in dotted_path.split("."):
        if not segment:
            raise ValueError(f"invalid empty segment in path '{dotted_path}'")
        if not isinstance(current, dict) or segment not in current:
            raise KeyError(dotted_path)
        current = current[segment]
    if not isinstance(current, dict):
        raise ValueError(f"path is not a map: {dotted_path}")
    return current


class YamlDocs:
    """Loads YAML files on demand and tracks which were modified."""

    def __init__(self, chart_root: Path) -> None:
        self._root = chart_root
        self._yaml = YAML(typ="rt")
        self._yaml.preserve_quotes = True
        self._yaml.width = 4096
        self._yaml.indent(mapping=2, sequence=4, offset=2)
        self._docs: dict[Path, Any] = {}
        self._dirty: set[Path] = set()

    def get(self, rel_path: str) -> Any:
        path = self._root / rel_path
        if path not in self._docs:
            self._docs[path] = self._yaml.load(path.read_text(encoding="utf-8"))
        return self._docs[path]

    def mark_dirty(self, rel_path: str) -> None:
        self._dirty.add(self._root / rel_path)

    def flush(self) -> bool:
        for path in self._dirty:
            with path.open("w", encoding="utf-8") as handle:
                self._yaml.dump(self._docs[path], handle)
        return bool(self._dirty)


def set_dependency_version(chart: dict[str, Any], dep_name: str, version: str) -> bool:
    for dependency in chart.get("dependencies", []):
        if dependency.get("name") == dep_name:
            if dependency.get("version") != version:
                dependency["version"] = version
                return True
            return False
    raise ValueError(f"dependency {dep_name} not found in umbrella Chart.yaml")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update truefoundry chart values and version")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--tag-map", required=True)
    parser.add_argument("--components-path", default=str(COMPONENTS_PATH))
    # Root under which all chart files (umbrella + subcharts) resolve.
    parser.add_argument("--chart-root", default=".")
    args = parser.parse_args()

    components_path = Path(args.components_path)
    chart_root = Path(args.chart_root)

    spec = json.loads(args.spec)
    tag_map = json.loads(args.tag_map)
    components = {entry["repository"]: entry for entry in load_components(components_path)}

    chart_version = spec.get("chart_version")
    if not isinstance(chart_version, str) or not chart_version:
        raise ValueError("spec.chart_version is required")

    docs = YamlDocs(chart_root)

    def set_image_tag(values_file: str, image_path: str, tag: str) -> bool:
        values = docs.get(values_file)
        image_map = walk_to_map(values, image_path)
        if image_map.get("tag") != tag:
            image_map["tag"] = tag
            return True
        return False

    for repo, tag in tag_map.items():
        component = components.get(repo)
        if not component:
            raise ValueError(f"missing component mapping for repo {repo}")

        subchart = component.get("subchart")
        if subchart:
            # Standalone/nested subchart: bump its values image.tag + its Chart.yaml
            # version, and keep the umbrella dependency version in sync.
            values_file = subchart["values_file"]
            image_path = subchart.get("image_path", "image")
            if set_image_tag(values_file, image_path, tag):
                docs.mark_dirty(values_file)

            # A subchart is versioned INDEPENDENTLY of the umbrella: its image tag
            # follows its own per-repo line (e.g. gateway v0.154.1 while the
            # umbrella ships 0.154.2). Its CHART version — and the umbrella's
            # dependency pin on it — must track that SAME per-repo line, not the
            # umbrella chart version. Forcing them to the umbrella version skipped
            # the subchart's own line (0.154.0 -> 0.154.2) and shipped a chart
            # whose version disagreed with the image it carried (chart 0.154.2 +
            # image v0.154.1). Derive the chart version from the image tag (strip
            # the leading 'v'); image tags carry 'v', chart versions don't.
            subchart_version = tag.lstrip("v")

            chart_file = subchart["chart_file"]
            subchart_chart = docs.get(chart_file)
            if subchart_chart.get("version") != subchart_version:
                subchart_chart["version"] = subchart_version
                docs.mark_dirty(chart_file)

            # Some subcharts carry their own runtime version label under
            # `global.<key>` (e.g. tfy-llm-gateway's global.gatewayChartVersion,
            # surfaced as GATEWAY_VERSION). It must equal the subchart's OWN
            # Chart.yaml version (set above) — the gateway reports its own version
            # at runtime, on its own line, not the umbrella's.
            global_version_key = subchart.get("global_version_key")
            if global_version_key:
                subchart_values = docs.get(values_file)
                subchart_global = subchart_values.get("global")
                if not isinstance(subchart_global, dict):
                    raise ValueError(
                        f"{values_file} is missing the `global` map; cannot set "
                        f"global.{global_version_key}",
                    )
                if subchart_global.get(global_version_key) != subchart_version:
                    subchart_global[global_version_key] = subchart_version
                    docs.mark_dirty(values_file)

            # The umbrella depends on the subchart at the subchart's OWN version
            # (what actually gets published to OCI / bundled), not the umbrella
            # version — otherwise the dependency would point at a chart version
            # that was never published.
            umbrella_chart = docs.get(UMBRELLA_CHART)
            if set_dependency_version(umbrella_chart, subchart["name"], subchart_version):
                docs.mark_dirty(UMBRELLA_CHART)
            continue

        subpath = component.get("helm_subpath")
        subpaths = [subpath] if isinstance(subpath, str) else list(subpath or [])
        if not subpaths:
            raise ValueError(
                f"component {repo} has neither helm_subpath nor subchart",
            )
        for dotted in subpaths:
            if set_image_tag(UMBRELLA_VALUES, f"{dotted}.image", tag):
                docs.mark_dirty(UMBRELLA_VALUES)

    umbrella_chart = docs.get(UMBRELLA_CHART)
    if umbrella_chart.get("version") != chart_version:
        umbrella_chart["version"] = chart_version
        docs.mark_dirty(UMBRELLA_CHART)

    # B2: `global.controlPlaneChartVersion` is injected into pods at runtime as
    # the version label. It must be bumped every release (even when no image
    # tags changed) — the old manual flow handled this by hand. Without this
    # bump, prod pods would report a stale rc / wrong version after promotion.
    umbrella_values = docs.get(UMBRELLA_VALUES)
    global_block = umbrella_values.get("global")
    if not isinstance(global_block, dict):
        raise ValueError(
            "umbrella values.yaml is missing the `global` map; "
            "cannot set controlPlaneChartVersion",
        )
    if global_block.get("controlPlaneChartVersion") != chart_version:
        global_block["controlPlaneChartVersion"] = chart_version
        docs.mark_dirty(UMBRELLA_VALUES)

    changed = docs.flush()
    print(f"changed={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"error={error}", file=sys.stderr)
        raise
