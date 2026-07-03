import os
import subprocess

import yaml

ARTIFACTORY_REPOSITORY_URL = os.getenv("ARTIFACTORY_REPOSITORY_URL")
INFRAMOLD_ARTIFACTORY_REPOSITORY_URL = os.getenv("INFRAMOLD_ARTIFACTORY_REPOSITORY_URL")
CHARTS_TO_RELEASE = os.getenv("CHARTS_TO_RELEASE", "")
RELEASE_CHARTS = os.getenv("RELEASE_CHARTS", "")


def get_chart_info(chart_dir):
    """Extract chart name and version from Chart.yaml."""
    with open(os.path.join(chart_dir, "Chart.yaml"), "r", encoding="utf-8") as file:
        chart_yaml = yaml.safe_load(file)
        chart_name = chart_yaml.get("name")
        chart_version = chart_yaml.get("version")
    return chart_name, chart_version


def helm_chart_exists(chart_name, chart_version, repo_url):
    """Check if a Helm chart exists in the repository."""
    try:
        subprocess.run(
            ["helm", "pull", f"oci://{repo_url}/{chart_name}", "--version", chart_version],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def upload_chart(chart_dir):
    """Upload Helm chart to OCI repositories."""
    chart_name, chart_version = get_chart_info(chart_dir)
    print(f"Uploading chart {chart_name} with version {chart_version}")

    for repo_url in [ARTIFACTORY_REPOSITORY_URL, INFRAMOLD_ARTIFACTORY_REPOSITORY_URL]:
        if not repo_url:
            # Optional registry not configured; skip instead of pushing to oci://None.
            continue
        if helm_chart_exists(chart_name, chart_version, repo_url):
            print(f"{chart_name}-{chart_version} already exists in oci://{repo_url}")
            continue

        subprocess.run(["helm", "dependency", "update"], check=True, cwd=chart_dir)
        subprocess.run(["helm", "package", "."], check=True, cwd=chart_dir)

        package_file = f"{chart_name}-{chart_version}.tgz"
        try:
            subprocess.run(
                ["helm", "push", package_file, f"oci://{repo_url}"],
                check=True,
                cwd=chart_dir,
            )
            print(f"Pushed helm chart {package_file} to oci://{repo_url}")
        except subprocess.CalledProcessError as error:
            print(f"Failed to push helm chart {chart_name}: {error}")
            raise


def get_chart_dirs():
    current_dir = os.getcwd()
    charts = CHARTS_TO_RELEASE.split() or RELEASE_CHARTS.split()
    return [os.path.join(current_dir, "charts", chart) for chart in charts]


def main():
    """Upload selected chart directories."""
    for chart_dir in get_chart_dirs():
        if not os.path.isdir(chart_dir):
            raise FileNotFoundError(f"Chart directory not found: {chart_dir}")
        print(f"Current directory is {chart_dir}")
        upload_chart(chart_dir)


if __name__ == "__main__":
    main()
