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

`fork_and_clone`, `subscribe`, and `subscribe_to_riscv64_prs` back Phase 1 Verify's automatic
fork/clone-and-subscribe behavior (previously a Phase-3-only, human-run procedure documented in
CLAUDE.md): `cf_core.conda_forge_yml_check.check_feedstock` calls `fork_and_clone` when a
feedstock has no local clone yet, then `subscribe_to_riscv64_prs` to pick up notifications for
every riscv64-related PR on it, not just a specific PR number a caller already knew about. All
three are idempotent/safe to call every analysis run.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

from cf_core import migration_source as ms


def _run(args: list[str], timeout: int = 30, cwd: Optional[str] = None) -> Optional[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
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


def subscribe_to_riscv64_prs(repo: str, limit: int = 20) -> list[dict]:
    """Find every open PR on `repo` that looks riscv64-related and subscribe to each one's
    notifications. Broader than "the one bot PR and one best PR already known from the migration
    page" -- a bare `search_prs(repo, "riscv64")` also catches independent human PRs the
    migration-page scoring wouldn't necessarily surface as `best_pr_url`, and doesn't require the
    caller to already know a PR number (see `cf_core.conda_forge_yml_check.check_feedstock`,
    which calls this during Phase 1 Verify, before any PR has been looked at yet).

    `state="open"` deliberately -- a closed/merged PR isn't "getting contributions" any more, and
    subscribing to every historical riscv64 PR across the whole migration would be noise, not
    signal.

    Returns one {"number", "title", "subscribed"} dict per matching PR found. `subscribed: False`
    means the `gh api` PUT itself failed for that PR (rate limit, permissions, etc.), not that
    the PR wasn't riscv64-related -- it's still included so callers/logs see what was missed.
    """
    prs = search_prs(repo, "riscv64", state="open", limit=limit)
    results = []
    for pr in prs:
        number = pr.get("number")
        if number is None:
            continue
        results.append({
            "number": number,
            "title": pr.get("title", ""),
            "subscribed": subscribe(repo, number),
        })
    return results


def fork_and_clone(feedstock: str, conda_forge_root: Optional[str] = None) -> dict:
    """Fork <feedstock>-feedstock and clone it locally, using the exact command documented in
    CLAUDE.md's Phase-3 "Contributing to a feedstock" procedure -- promoted here from "a command
    a human runs by hand in Phase 3" to something Phase 1-2 analysis does automatically, up front,
    for every feedstock it looks at, so the local clone + PR checkout groundwork is already in
    place by the time anyone (human or a future Phase 3 run) needs it.

    Idempotent: if `<conda_forge_root>/<feedstock>-feedstock` already exists, this is a no-op --
    `gh repo fork --clone` is not safely re-runnable against an existing directory (it errors),
    and re-forking an already-forked repo serves no purpose.

    `conda_forge_root` defaults to `cf_core.migration_source.default_conda_forge_root()` (the
    parent of cwd) when omitted -- same default every other conda_forge_root-accepting function
    in cf_core uses, so callers only need to pass it explicitly when deviating from the standard
    `.bot`-lives-inside-`conda-forge/` layout.

    Returns {"feedstock", "path", "already_cloned", "ok", "error"}.
    """
    # Path convention lives in cf_core.migration_source (not re-derived here) so this function
    # -- which CREATES the clone -- and conda_forge_yml_check -- which LOOKS for it afterwards --
    # can never independently drift on where a feedstock's local clone is supposed to live.
    conda_forge_root = conda_forge_root or ms.default_conda_forge_root()
    clone_path = ms.local_clone_path(feedstock, conda_forge_root)
    if os.path.isdir(clone_path):
        return {
            "feedstock": feedstock,
            "path": clone_path,
            "already_cloned": True,
            "ok": True,
            "error": None,
        }

    out = _run([
        "gh", "repo", "fork", "--clone", "--fork-name", f"conda-forge-{feedstock}-feedstock",
        f"https://github.com/conda-forge/{feedstock}-feedstock", f"{feedstock}-feedstock",
    ], timeout=120, cwd=conda_forge_root)
    ok = out is not None
    return {
        "feedstock": feedstock,
        "path": clone_path,
        "already_cloned": False,
        "ok": ok,
        "error": None if ok else "gh repo fork --clone failed (see gh's own output/auth state)",
    }
