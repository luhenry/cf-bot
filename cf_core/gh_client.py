"""
cf_core.gh_client — thin wrappers around the `gh` CLI for the calls cf-bot needs.

Before this module existed, `gh pr view`/`gh api ...` calls were written as raw shell-command
text embedded directly inside LLM agent prompts in analyze_feedstock.js, with zero fallback and
zero shared implementation with the equivalent calls in riscv64_status.py (`gh_list_open_prs`,
`repo_already_uses_v1`). This module is the one implementation; the JS workflows call it via a
`cf_core` CLI relay instead of asking an LLM agent to construct `gh` invocations itself.

Assumes `gh` is installed and authenticated in the execution environment -- true for cf-bot's
real cron/execution environment. If `gh` is missing/unauthenticated/rate-limited, these functions
fail soft (empty list / None), matching the existing behavior in riscv64_status.py, rather than
raising -- callers that need to distinguish "no results" from "gh itself is broken" should check
the environment directly (e.g. `shutil.which("gh")`) before calling in.
"""
from __future__ import annotations

import json
import subprocess
from typing import Optional


def _run(args: list[str], timeout: int = 30) -> Optional[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def list_open_prs(repo: str) -> list[dict]:
    out = _run([
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,author,createdAt,url,body,files",
    ])
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def search_prs(repo: str, search: str, state: str = "all", limit: int = 5) -> list[dict]:
    out = _run([
        "gh", "pr", "list", "--repo", repo, "--state", state, "--search", search,
        "--json", "number,title,author,state,isDraft,url", "--limit", str(limit),
    ])
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def repo_uses_v1_recipe(repo: str) -> bool:
    out = _run(["gh", "api", f"repos/{repo}/contents/recipe", "--jq", ".[].name"])
    return bool(out) and "recipe.yaml" in out


def pr_view(repo: str, number: int, fields: str) -> Optional[dict]:
    out = _run(["gh", "pr", "view", str(number), "--repo", repo, "--json", fields])
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def pr_comments(repo: str, number: int) -> list[dict]:
    out = _run([
        "gh", "api", f"repos/{repo}/issues/{number}/comments",
        "--jq", ".[] | {author: .user.login, date: .created_at, body: .body}",
    ])
    if not out:
        return []
    comments = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            comments.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return comments


def subscribe(repo: str, issue_or_pr_number: int) -> bool:
    """Equivalent of clicking "Subscribe" on the PR's web UI -- there's no dedicated `gh`
    subcommand for this, only the raw API. Returns True on a 200 response."""
    out = _run([
        "gh", "api", "-X", "PUT", f"repos/{repo}/issues/{issue_or_pr_number}/subscription",
        "-f", "subscribed=true", "-f", "ignored=false",
    ])
    return out is not None
