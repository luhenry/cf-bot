#!/usr/bin/env python3
"""
Analyze the linux-riscv64 migration status and find the best PR for each ready project.

A "ready" project is one that:
- Has an open PR (is in the 'in-pr' category)
- All its dependencies are already done
"""
import json
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
         "--json", "number,title,author,createdAt,url"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout)


def best_pr(prs: list[dict], bot_pr_number: int) -> Optional[dict]:
    """
    Pick the best PR for a migration:
    1. A non-bot PR that mentions riscv64 (user fix)
    2. The bot's own PR
    """
    bot_pr = None
    user_riscv_prs = []
    for pr in prs:
        is_bot = pr["author"]["login"] == BOT_AUTHOR
        is_riscv = any(kw in pr["title"].lower() for kw in RISCV_KEYWORDS)
        if is_bot and pr["number"] == bot_pr_number:
            bot_pr = pr
        elif not is_bot and is_riscv:
            user_riscv_prs.append(pr)

    if user_riscv_prs:
        # newest first
        user_riscv_prs.sort(key=lambda p: p["createdAt"], reverse=True)
        return user_riscv_prs[0]
    return bot_pr


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
        }

        if fetch_prs and bot_pr_number is not None and pr_status == "unstable":
            repo = f"conda-forge/{name}-feedstock"
            prs = gh_list_open_prs(repo)
            chosen = best_pr(prs, bot_pr_number)
            if chosen:
                result["best_pr_url"] = chosen["url"]
                result["best_pr_author"] = chosen["author"]["login"]

        ready.append(result)

    ready.sort(key=lambda x: x["num_descendants"], reverse=True)
    return ready


def main():
    fetch_prs = "--no-gh" not in sys.argv
    print(f"Fetching migration data{'  + checking GitHub PRs' if fetch_prs else ''}...\n")
    ready = analyze(fetch_prs=fetch_prs)

    print(f"Ready projects (in-PR, all deps done): {len(ready)}\n")
    print(f"{'Package':<30} {'Status':<10} {'Descendants':>11}  Best PR")
    print("-" * 100)
    for p in ready:
        author_note = f" [{p['best_pr_author']}]" if p["best_pr_author"] != BOT_AUTHOR else ""
        print(f"{p['name']:<30} {p['pr_status']:<10} {p['num_descendants']:>11}  {p['best_pr_url']}{author_note}")


if __name__ == "__main__":
    main()
