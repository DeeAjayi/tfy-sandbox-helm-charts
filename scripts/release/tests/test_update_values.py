from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ruamel.yaml import YAML


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "update_values.py"
)
SPEC = importlib.util.spec_from_file_location("update_values", MODULE_PATH)
update_values = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(update_values)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _setup_chart(root: Path) -> Path:
    """Build a minimal chart tree and components file; returns components path."""
    components = root / "config" / "components.yml"
    _write(
        components,
        """
repositories:
  - repository: truefoundry/servicefoundry-server
    helm_subpath: servicefoundryServer
  - repository: truefoundry/deltafusion-ingestor
    helm_subpath:
      - deltaFusionIngestor
      - deltaFusionCompaction
  - repository: truefoundry/tfy-llm-gateway
    subchart:
      name: tfy-llm-gateway
      values_file: charts/tfy-llm-gateway/values.yaml
      image_path: image
      chart_file: charts/tfy-llm-gateway/Chart.yaml
      global_version_key: gatewayChartVersion
  - repository: truefoundry/tfy-otel-collector
    subchart:
      name: tfy-otel-collector
      values_file: charts/truefoundry/charts/tfy-otel-collector/values.yaml
      image_path: image
      chart_file: charts/truefoundry/charts/tfy-otel-collector/Chart.yaml
""",
    )
    _write(
        root / "charts/truefoundry/values.yaml",
        """
global:
  controlPlaneChartVersion: 0.149.0-rc.1
servicefoundryServer:
  image:
    tag: old-tag
deltaFusionIngestor:
  image:
    tag: old-tag
deltaFusionCompaction:
  image:
    tag: old-tag
""",
    )
    _write(
        root / "charts/truefoundry/Chart.yaml",
        """
apiVersion: v2
name: truefoundry
version: 0.149.0-rc.1
dependencies:
  - name: tfy-llm-gateway
    version: 0.149.0-rc.1
  - name: tfy-otel-collector
    version: 0.149.0-rc.1
""",
    )
    _write(
        root / "charts/tfy-llm-gateway/values.yaml",
        "global:\n  gatewayChartVersion: 0.149.0-rc.1\n"
        "image:\n  repository: tfy-private-images/tfy-llm-gateway\n  tag: v0.149.0-rc.1\n",
    )
    _write(
        root / "charts/tfy-llm-gateway/Chart.yaml",
        "apiVersion: v2\nname: tfy-llm-gateway\nversion: 0.149.0-rc.1\n",
    )
    _write(
        root / "charts/truefoundry/charts/tfy-otel-collector/values.yaml",
        "image:\n  repository: tfy-private-images/tfy-otel-collector\n  tag: v0.149.0-rc.1\n",
    )
    _write(
        root / "charts/truefoundry/charts/tfy-otel-collector/Chart.yaml",
        "apiVersion: v2\nname: tfy-otel-collector\nversion: 0.149.0-rc.1\n",
    )
    return components


def _run(components: Path, root: Path, spec: dict, tag_map: dict, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "update_values.py",
            "--spec",
            json.dumps(spec),
            "--tag-map",
            json.dumps(tag_map),
            "--components-path",
            str(components),
            "--chart-root",
            str(root),
        ],
    )
    assert update_values.main() == 0


def test_update_values_umbrella_and_subcharts(tmp_path, monkeypatch):
    components = _setup_chart(tmp_path)
    spec = {"chart_version": "0.149.0-rc.2"}
    tag_map = {
        "truefoundry/servicefoundry-server": "v0.149.0-rc.2",
        "truefoundry/deltafusion-ingestor": "v0.149.0-rc.2",
        "truefoundry/tfy-llm-gateway": "v0.149.0-rc.2",
        "truefoundry/tfy-otel-collector": "v0.149.0-rc.2",
    }
    _run(components, tmp_path, spec, tag_map, monkeypatch)

    yaml = YAML(typ="safe")
    umbrella_values = yaml.load((tmp_path / "charts/truefoundry/values.yaml").read_text())
    umbrella_chart = yaml.load((tmp_path / "charts/truefoundry/Chart.yaml").read_text())
    gw_values = yaml.load((tmp_path / "charts/tfy-llm-gateway/values.yaml").read_text())
    gw_chart = yaml.load((tmp_path / "charts/tfy-llm-gateway/Chart.yaml").read_text())
    otel_values = yaml.load(
        (tmp_path / "charts/truefoundry/charts/tfy-otel-collector/values.yaml").read_text(),
    )
    otel_chart = yaml.load(
        (tmp_path / "charts/truefoundry/charts/tfy-otel-collector/Chart.yaml").read_text(),
    )

    # umbrella image-tag subpaths
    assert umbrella_values["servicefoundryServer"]["image"]["tag"] == "v0.149.0-rc.2"
    assert umbrella_values["deltaFusionIngestor"]["image"]["tag"] == "v0.149.0-rc.2"
    assert umbrella_values["deltaFusionCompaction"]["image"]["tag"] == "v0.149.0-rc.2"
    assert umbrella_chart["version"] == "0.149.0-rc.2"
    # B2: global.controlPlaneChartVersion must also be bumped per release so
    # pods report the right runtime version label.
    assert umbrella_values["global"]["controlPlaneChartVersion"] == "0.149.0-rc.2"

    # gateway: top-level chart values image.tag + its Chart.yaml + umbrella dep
    assert gw_values["image"]["tag"] == "v0.149.0-rc.2"
    # global.gatewayChartVersion (runtime GATEWAY_VERSION) bumps with the chart.
    assert gw_values["global"]["gatewayChartVersion"] == "0.149.0-rc.2"
    assert gw_chart["version"] == "0.149.0-rc.2"
    deps = {d["name"]: d["version"] for d in umbrella_chart["dependencies"]}
    assert deps["tfy-llm-gateway"] == "0.149.0-rc.2"

    # otel: nested subchart values image.tag + its Chart.yaml + umbrella dep
    assert otel_values["image"]["tag"] == "v0.149.0-rc.2"
    assert otel_chart["version"] == "0.149.0-rc.2"
    assert deps["tfy-otel-collector"] == "0.149.0-rc.2"


def test_subchart_version_tracks_own_line_not_umbrella(tmp_path, monkeypatch):
    """Regression: a subchart versioned on its OWN line (image v0.154.1) while the
    umbrella ships 0.154.2 must get its Chart.yaml version + umbrella dependency
    pin from the per-repo image tag (0.154.1), NOT the umbrella chart version.
    Previously both were forced to the umbrella version, producing a gateway
    chart 0.154.2 that shipped image v0.154.1 (and skipped its own 0.154.1)."""
    components = _setup_chart(tmp_path)
    # Umbrella advances to 0.154.2; gateway's own line is one patch behind.
    spec = {"chart_version": "0.154.2"}
    tag_map = {
        "truefoundry/servicefoundry-server": "v0.154.2",
        "truefoundry/tfy-llm-gateway": "v0.154.1",
    }
    _run(components, tmp_path, spec, tag_map, monkeypatch)

    yaml = YAML(typ="safe")
    umbrella_chart = yaml.load((tmp_path / "charts/truefoundry/Chart.yaml").read_text())
    gw_values = yaml.load((tmp_path / "charts/tfy-llm-gateway/values.yaml").read_text())
    gw_chart = yaml.load((tmp_path / "charts/tfy-llm-gateway/Chart.yaml").read_text())

    # Image tag + chart version now AGREE on the subchart's own line.
    assert gw_values["image"]["tag"] == "v0.154.1"
    assert gw_chart["version"] == "0.154.1"
    # Umbrella depends on the version actually published, not the umbrella's.
    deps = {d["name"]: d["version"] for d in umbrella_chart["dependencies"]}
    assert deps["tfy-llm-gateway"] == "0.154.1"
    # GATEWAY_VERSION runtime label equals the subchart's OWN Chart.yaml version.
    assert gw_values["global"]["gatewayChartVersion"] == "0.154.1"
    assert gw_values["global"]["gatewayChartVersion"] == gw_chart["version"]
    # Umbrella itself is unaffected.
    assert umbrella_chart["version"] == "0.154.2"
