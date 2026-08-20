#!/usr/bin/env python3
"""
Analyze the linux-riscv64 migration status and find the best PR for each ready project.

A "ready" project is one that:
- Has an open PR (is in the 'in-pr' category)
- All its dependencies are already done
"""
import json
import re
import subprocess
import sys
import urllib.request
from typing import Optional

MIGRATION_URL = "https://raw.githubusercontent.com/conda-forge/conda-forge-bot-data/main/status/migration_json/supportlinuxriscv64platform.json"
BOT_AUTHOR = "regro-cf-autotick-bot"
RISCV_KEYWORDS = ["riscv64", "riscv"]


def fetch_migration_data() -> dict:
    with urllib.request.urlopen(MIGRATION_URL) as r:
        return json.load(r)


def build_parents_map(feedstocks: dict) -> dict[str, list[str]]:
    """Invert immediate_children to get parent->children -> child->parents."""
    parents_of: dict[str, list[str]] = {name: [] for name in feedstocks}
    for name, info in feedstocks.items():
        for child in info.get("immediate_children", []):
            if child in parents_of:
                parents_of[child].append(name)
    return parents_of


def gh_list_open_prs(repo: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open",
         "--json", "number,title,author,createdAt,url,body,files"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout)


def repo_already_uses_v1(repo: str) -> bool:
    """True if the repo's main branch already has recipe/recipe.yaml."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/recipe", "--jq", ".[].name"],
        capture_output=True, text=True
    )
    return "recipe.yaml" in result.stdout


def pr_is_v1_migration(pr: dict, repo_is_v1: bool) -> bool:
    """True if the PR introduces v1/rattler-build format (not just uses existing one)."""
    if repo_is_v1:
        return False  # repo already uses v1, no migration happening
    files = [f["path"] for f in pr.get("files", [])]
    has_recipe_yaml = any("recipe.yaml" in f for f in files)
    title_v1 = any(kw in pr["title"].lower() for kw in ["v1", "rattler", "pixi"])
    return has_recipe_yaml or title_v1


def pr_supersedes(pr: dict, bot_pr_number: int) -> bool:
    """True if the PR body explicitly claims to supersede the bot PR."""
    body = pr.get("body") or ""
    # "Supersedes #19", "supersedes and includes ... #19", etc.
    matches = re.findall(r"[Ss]upersedes[^#\n]*#(\d+)", body)
    return str(bot_pr_number) in matches


def score_pr(pr: dict, bot_pr_number: int, repo_is_v1: bool) -> int:
    """
    Higher score = better candidate to focus on.

    Priority logic:
    1. Non-bot, riscv64-related, NOT a v1 migration (minimal focused fix)  → 100+
       + bonus if it explicitly supersedes the bot PR                       → +20
    2. Bot PR (always available as fallback)                                → 20
    3. Non-bot, riscv64-related, IS a v1 migration (broader scope)         → 10+
       + bonus if it explicitly supersedes the bot PR                       → +5

    Rationale: a v1 migration is broader scope and harder to review/merge;
    prefer the bot PR as reference when no focused fix exists.
    Within each tier, newer PRs rank higher (used as tiebreak externally).
    """
    is_bot = pr["author"]["login"] == BOT_AUTHOR
    is_riscv = any(kw in pr["title"].lower() for kw in RISCV_KEYWORDS)

    if is_bot:
        return 20  # preferred over v1-only alternatives

    if not is_riscv:
        return 0  # unrelated PR, ignore

    is_v1 = pr_is_v1_migration(pr, repo_is_v1)
    if is_v1:
        score = 10
        if pr_supersedes(pr, bot_pr_number):
            score += 5
        return score

    # Focused riscv64 fix (best)
    score = 100
    if pr_supersedes(pr, bot_pr_number):
        score += 20
    return score


def best_pr(prs: list[dict], bot_pr_number: int, repo_is_v1: bool) -> Optional[dict]:
    """Return the PR most relevant to the riscv64 migration."""
    scored = []
    for pr in prs:
        s = score_pr(pr, bot_pr_number, repo_is_v1)
        if s > 0:
            scored.append((s, pr["createdAt"], pr))

    if not scored:
        return None

    # Sort by score desc, then creation date desc (newer wins ties)
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def analyze(fetch_prs: bool = True) -> list[dict]:
    data = fetch_migration_data()
    feedstocks = data["_feedstock_status"]
    done_set = set(data["done"])
    in_pr_set = set(data["in-pr"])

    parents_of = build_parents_map(feedstocks)

    ready = []
    for name in sorted(in_pr_set):
        if name not in feedstocks:
            continue
        info = feedstocks[name]
        deps = parents_of.get(name, [])
        undone_deps = [d for d in deps if d not in done_set]
        if undone_deps:
            continue

        pr_url = info.get("pr_url", "")
        pr_status = info.get("pr_status", "")
        bot_pr_number = int(pr_url.rstrip("/").split("/")[-1]) if "/pull/" in pr_url else None

        result = {
            "name": name,
            "bot_pr_url": pr_url,
            "pr_status": pr_status,
            "num_descendants": info.get("num_descendants", 0),
            "best_pr_url": pr_url,
            "best_pr_author": BOT_AUTHOR,
            "best_pr_is_v1": False,
        }

        if fetch_prs and bot_pr_number is not None and pr_status == "unstable":
            repo = f"conda-forge/{name}-feedstock"
            prs = gh_list_open_prs(repo)
            repo_is_v1 = repo_already_uses_v1(repo)
            chosen = best_pr(prs, bot_pr_number, repo_is_v1)
            if chosen and chosen["author"]["login"] != BOT_AUTHOR:
                result["best_pr_url"] = chosen["url"]
                result["best_pr_author"] = chosen["author"]["login"]
                result["best_pr_is_v1"] = pr_is_v1_migration(chosen, repo_is_v1)

        ready.append(result)

    ready.sort(key=lambda x: x["num_descendants"], reverse=True)
    return ready


def main():
    fetch_prs = "--no-gh" not in sys.argv
    print(f"Fetching migration data{'  + checking GitHub PRs' if fetch_prs else ''}...\n")
    ready = analyze(fetch_prs=fetch_prs)

    print(f"Ready projects (in-PR, all deps done): {len(ready)}\n")
    print(f"{'Package':<30} {'Status':<10} {'Desc':>5}  Best PR")
    print("-" * 110)
    for p in ready:
        author = p["best_pr_author"]
        note = ""
        if author != BOT_AUTHOR:
            note = f" [{author}{'  v1' if p['best_pr_is_v1'] else ''}]"
        print(f"{p['name']:<30} {p['pr_status']:<10} {p['num_descendants']:>5}  {p['best_pr_url']}{note}")


if __name__ == "__main__":
    main()
