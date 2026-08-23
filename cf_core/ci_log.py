"""
cf_core.ci_log — fetch the tail of a CI build log given a check URL.

Moved from the top-level fetch_ci_log.py as-is (same behavior, same URL formats supported); the
repo-root fetch_ci_log.py is now a thin CLI shim over this module so `python3 fetch_ci_log.py ...`
keeps working unchanged for anyone/anything with that command memorized.

Supports:
  - GitHub Actions: https://github.com/<org>/<repo>/actions/runs/<runId>/job/<jobId>
  - Azure Pipelines: https://dev.azure.com/<org>/<project>/_build/results?buildId=<id>[&jobId=<jobId>]
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from typing import Optional


def fetch_github_actions_log(run_id: str, job_id: str, repo: str, lines: int) -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"[error fetching GHA log: {result.stderr.strip()[:200]}]"
    tail = result.stdout.strip().splitlines()[-lines:]
    return "\n".join(tail)


def fetch_azure_log(org: str, project: str, build_id: str, job_id: Optional[str], lines: int) -> str:
    base = f"https://dev.azure.com/{org}/{project}/_apis/build/builds/{build_id}"
    timeline_url = f"{base}/timeline?api-version=7.0"

    with urllib.request.urlopen(timeline_url) as r:
        timeline = json.load(r)

    records = timeline.get("records", [])

    target_log_id = None
    if job_id:
        for rec in records:
            if rec.get("id", "").startswith(job_id) and rec.get("type") == "Job":
                log = rec.get("log") or {}
                target_log_id = log.get("id")
                break
    if target_log_id is None:
        for rec in records:
            if rec.get("result") in ("failed", "partiallySucceeded") and rec.get("type") == "Job":
                log = rec.get("log") or {}
                target_log_id = log.get("id")
                break

    if target_log_id is None:
        task_logs = []
        for rec in records:
            if rec.get("result") == "failed" and rec.get("type") == "Task":
                log = rec.get("log") or {}
                lid = log.get("id")
                if lid:
                    task_logs.append((lid, rec.get("name", "")))
        if task_logs:
            task_logs.sort(reverse=True)
            target_log_id = task_logs[0][0]

    if target_log_id is None:
        return "[no failed job found in Azure timeline]"

    log_url = f"{base}/logs/{target_log_id}?api-version=7.0"
    with urllib.request.urlopen(log_url) as r:
        content = r.read().decode("utf-8", errors="replace")
    tail = content.strip().splitlines()[-lines:]
    return "\n".join(tail)


def fetch_log(url: str, lines: int = 80) -> str:
    m = re.search(r"github\.com/([^/]+/[^/]+)/actions/runs/(\d+)/job/(\d+)", url)
    if m:
        repo, run_id, job_id = m.group(1), m.group(2), m.group(3)
        return fetch_github_actions_log(run_id, job_id, repo, lines)

    m = re.match(r"https://dev\.azure\.com/([^/]+)/([^/]+)/_build", url)
    if m:
        org, project = m.group(1), m.group(2)
        build_id_m = re.search(r"buildId=(\d+)", url)
        job_id_m = re.search(r"jobId=([\w-]+)", url)
        if not build_id_m:
            return "[Azure URL missing buildId]"
        return fetch_azure_log(
            org, project, build_id_m.group(1),
            job_id_m.group(1) if job_id_m else None, lines,
        )

    return f"[unsupported CI URL: {url}]"
