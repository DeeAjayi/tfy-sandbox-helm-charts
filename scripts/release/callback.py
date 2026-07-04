#!/usr/bin/env python3

"""callback.py — fire-and-forget HTTP POST to release-app.

Used by the release orchestrator workflows to push phase-level progress
updates back to release-app. Designed to NEVER block or fail the workflow:
  - 5 second hard timeout
  - any network/auth/parse error is logged and swallowed
  - exit code is always 0 unless invoked with invalid CLI args

Auth: Bearer token (env RELEASE_APP_CALLBACK_TOKEN, validated by release-app
against a shared secret). Workflow short-circuits cleanly when the env vars
are missing — useful in fork PRs or dev runs where the secrets aren't wired.

Run identity: we use $GITHUB_RUN_ID as the natural primary key. release-app's
`truefoundry_releases.gha_workflow_run_id` column maps a callback to its row;
if no row exists yet (orchestrator started manually via the GHA UI / gh CLI),
release-app's first-callback handler will insert one. No extra input plumbing is
required from the dispatcher.

When $GITHUB_RUN_ID is NOT set, we synthesize a fallback id (see
`_resolve_run_id`) so the callback is still sent — derived deterministically
from the other stable GITHUB_* env vars so every phase of the same run maps to
one release-app row, falling back to a random id only when no GitHub env is
present at all.

Payload shape (POST <RELEASE_APP_BASE_URL>/api/v1/releases/<github_run_id>/callback):
    {
      "github_run_id":  "9876543210",
      "github_run_url": "https://github.com/<repo>/actions/runs/9876543210",
      "phase":          "compute" | "branches" | "build" | "retag"
                        | "helm"    | "publish"  | "finalize",
      "status":         "success" | "failed" | "skipped" | "running",
      "spec":           {... the orchestration spec ...},
      "data":           {... phase-specific fields ...},
      "component_updates": [{repository, status, tag, commit_sha, ci_link}, ...]
    }

CLI:
    python3 scripts/release/callback.py \\
        --phase build --status success \\
        --spec "$SPEC" \\
        --data '{"tag_map": "..."}'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import uuid


TIMEOUT_SECONDS = 5

VALID_PHASES = (
    "compute",
    "branches",
    "build",
    "retag",
    "helm",
    "publish",
    "finalize",
    "deploy_develop",
    "deploy_prod",
    # --- end-to-end tail phases (see release-app design-docs/release/release-end-to-end.md) ---
    "verify_truefoundry_oci",
    "infra_truefoundry",
    "ubermold_develop",
    "purge_cache",
    "verify_gateway_ghpages",
    "gateway_dev",
    "gateway_prod_canary",
    "gateway_prod",
    "infra_inframold",
    "ubermold_backmerge",
    "verify_inframold",
)

VALID_STATUSES = ("success", "failed", "skipped", "running")


def _log(msg: str) -> None:
    print(f"[callback] {msg}", file=sys.stderr)


def _resolve_run_id() -> str:
    """The run identity release-app keys callbacks on.

    Prefer the real $GITHUB_RUN_ID. When it's absent (running outside Actions,
    a reusable-workflow/dispatch context that doesn't surface it, local runs),
    synthesize a stable fallback so the callback can still be sent and all
    phases of the SAME run map to one release-app row:

      1. $GITHUB_RUN_ID                          — the real key (always one row)
      2. gen-<hash of stable GITHUB_* env>       — deterministic across a run's
         jobs/steps (run number + attempt + repo + workflow + ref/sha)
      3. gen-<random uuid>                        — last resort (no GITHUB_* at
         all); may produce a separate row per invocation
    """
    real = (os.environ.get("GITHUB_RUN_ID") or "").strip()
    if real:
        return real

    stable_parts = [
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_WORKFLOW", ""),
        os.environ.get("GITHUB_RUN_NUMBER", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        os.environ.get("GITHUB_REF", ""),
        os.environ.get("GITHUB_SHA", ""),
    ]
    if any(part.strip() for part in stable_parts):
        digest = hashlib.sha1("|".join(stable_parts).encode("utf-8")).hexdigest()
        return f"gen-{digest[:20]}"

    return f"gen-{uuid.uuid4().hex[:20]}"


def _build_payload(args: argparse.Namespace, run_id: str) -> dict:
    payload: dict = {
        "github_run_id":  run_id,
        "github_run_url": _github_run_url(),
        "phase":          args.phase,
        "status":         args.status,
    }
    spec_text = (args.spec or "").strip()
    if spec_text:
        try:
            payload["spec"] = json.loads(spec_text)
        except json.JSONDecodeError as e:
            _log(f"warning: --spec is not valid JSON ({e}); sending as raw string")
            payload["spec"] = spec_text
    data_text = (args.data or "").strip()
    if data_text:
        try:
            payload["data"] = json.loads(data_text)
        except json.JSONDecodeError as e:
            _log(f"warning: --data is not valid JSON ({e}); sending as raw string")
            payload["data"] = data_text
    # Per-component results (build phase). Sent at top level so release-app's
    # CallbackDto.component_updates picks them up directly — this is what
    # populates the per-repo Step timeline + CI links in release-app.
    cu_text = (args.component_updates or "").strip()
    if cu_text:
        try:
            payload["component_updates"] = json.loads(cu_text)
        except json.JSONDecodeError as e:
            _log(
                f"warning: --component-updates is not valid JSON ({e}); omitting",
            )
    if args.error:
        payload["error_message"] = args.error
    return payload


def _github_run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repo   = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not (server and repo and run_id):
        return ""
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
    base = f"{server}/{repo}/actions/runs/{run_id}"
    return f"{base}/attempts/{attempt}" if attempt else base


def _post(url: str, token: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        # Same UA we use elsewhere in release tooling, helps grep server logs.
        "User-Agent":    "truefoundry-release-orchestrator/1.0",
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        status = response.status
        text = response.read().decode("utf-8", errors="replace")[:200]
        _log(f"POST {url} -> {status} {text!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify release-app of a phase event")
    parser.add_argument("--phase",  required=True, choices=VALID_PHASES)
    parser.add_argument("--status", required=True, choices=VALID_STATUSES)
    parser.add_argument(
        "--spec",
        default="",
        help="Orchestration spec as JSON string (the workflow's inputs.spec).",
    )
    parser.add_argument(
        "--data",
        default="",
        help="Phase-specific extras as JSON string (tag_map, commit_map, pr_url, etc.).",
    )
    parser.add_argument(
        "--component-updates",
        default="",
        help=(
            "JSON array of per-component results (build phase): "
            "[{repository, status, tag, commit_sha, ci_link}]. Sent top-level so "
            "release-app records per-repo timeline rows + CI links."
        ),
    )
    parser.add_argument(
        "--error",
        default="",
        help="Human-readable error message when status=failed.",
    )
    args = parser.parse_args()

    # Run identity is the natural primary key — release-app stores it as
    # `gha_workflow_run_id` and creates a row on the first orphan callback if
    # one doesn't already exist. We always have an id now: the real
    # $GITHUB_RUN_ID when present, otherwise a synthesized "gen-" fallback so the
    # callback still fires when GITHUB_RUN_ID isn't available.
    run_id = _resolve_run_id()
    base_url = (os.environ.get("RELEASE_APP_BASE_URL") or "").strip().rstrip("/")
    token = (os.environ.get("RELEASE_APP_CALLBACK_TOKEN") or "").strip()

    if not base_url or not token:
        # No release-app wired in (fork PR, dev run without secrets, etc.).
        # Log and exit 0 — callbacks are opportunistic, never required.
        _log(
            f"skip: phase={args.phase} status={args.status} "
            f"(RELEASE_APP_BASE_URL/RELEASE_APP_CALLBACK_TOKEN not set)",
        )
        return 0

    url = f"{base_url}/api/v1/releases/{run_id}/callback"
    payload = _build_payload(args, run_id)
    generated = not (os.environ.get("GITHUB_RUN_ID") or "").strip()
    _log(
        f"sending phase={args.phase} status={args.status} run_id={run_id}"
        f"{' (generated)' if generated else ''}",
    )
    try:
        _post(url, token, payload)
    except urllib.error.HTTPError as e:
        # Server reachable but rejected — log status + body, never raise.
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        _log(f"warning: server returned {e.code}: {body}")
    except urllib.error.URLError as e:
        _log(f"warning: network error: {e.reason}")
    except (TimeoutError, OSError) as e:
        _log(f"warning: io/timeout: {e}")
    except Exception as e:  # noqa: BLE001
        # Defense in depth — anything truly unexpected, still don't break the workflow.
        _log(f"warning: unexpected error: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
