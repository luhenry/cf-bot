#!/usr/bin/env python3
"""
Analyze the linux-riscv64 migration status and find the best PR for each ready project.

A "ready" project is one that:
- Has an open PR (is in the 'in-pr' category)
- All its dependencies are already done

This is now a thin backward-compatible CLI over cf_core -- all the actual logic (graph
traversal, CUDA/v1-bundle policy, PR scoring) lives in cf_core/ and is shared with the JS
workflows via `python3 -m cf_core ...`. Kept as a top-level script (same name, same flags,
same output format) so any existing muscle memory / cron entry referencing
`python3 riscv64_status.py --depth` keeps working unchanged. See CLAUDE.md.
"""
import sys
from typing import Optional

from cf_core import gh_client
from cf_core import graph as g
from cf_core import migration_source as ms
from cf_core import policy


def best_pr(prs: list[dict], bot_pr_number: Optional[int], repo_is_v1: bool) -> Optional[dict]:
    scored = []
    for pr in prs:
        s = policy.score_pr(
            author_login=pr["author"]["login"],
            title=pr["title"],
            body=pr.get("body"),
            changed_files=[f["path"] for f in pr.get("files", [])],
            bot_pr_number=bot_pr_number or -1,
            repo_already_uses_v1=repo_is_v1,
        )
        if s > 0:
            scored.append((s, pr["createdAt"], pr))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


def analyze(fetch_prs: bool = True, with_depth: bool = False) -> list[dict]:
    data = ms.fetch_migration_json()
    feedstocks = data["_feedstock_status"]
    done_set = set(data["done"])
    in_pr_set = set(data["in-pr"])

    G = g.build_graph(feedstocks)
    depths = g.depth_to_target(G, policy.CI_SETUP_TARGET) if with_depth else {}

    ready = []
    for name in sorted(in_pr_set):
        if name not in feedstocks:
            continue
        info = feedstocks[name]
        deps = g.parents_of(G, name)
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
            "best_pr_author": policy.BOT_AUTHOR,
            "best_pr_is_v1": False,
            "depth_to_ci_setup": depths.get(name) if with_depth else None,
            "is_cuda_wontfix": policy.is_cuda_wontfix(name),
        }

        if fetch_prs and bot_pr_number is not None and pr_status == "unstable":
            repo = f"conda-forge/{name}-feedstock"
            prs = gh_client.list_open_prs(repo)
            repo_is_v1 = gh_client.repo_uses_v1_recipe(repo)
            chosen = best_pr(prs, bot_pr_number, repo_is_v1)
            if chosen and chosen["author"]["login"] != policy.BOT_AUTHOR:
                result["best_pr_url"] = chosen["url"]
                result["best_pr_author"] = chosen["author"]["login"]
                result["best_pr_is_v1"] = policy.pr_is_v1_migration(
                    chosen["title"], [f["path"] for f in chosen.get("files", [])], repo_is_v1
                )

        ready.append(result)

    if with_depth:
        ready.sort(key=lambda x: (
            x["depth_to_ci_setup"] if x["depth_to_ci_setup"] is not None else float("inf"),
            -x["num_descendants"],
        ))
    else:
        ready.sort(key=lambda x: x["num_descendants"], reverse=True)
    return ready


def main():
    fetch_prs = "--no-gh" not in sys.argv
    with_depth = "--depth" in sys.argv
    print(f"Fetching migration data{'  + checking GitHub PRs' if fetch_prs else ''}"
          f"{'  + computing depth to ' + policy.CI_SETUP_TARGET if with_depth else ''}...\n")
    ready = analyze(fetch_prs=fetch_prs, with_depth=with_depth)

    print(f"Ready projects (in-PR, all deps done): {len(ready)}\n")
    if with_depth:
        print(f"{'Package':<30} {'Status':<10} {'Depth':>5} {'Desc':>5}  Best PR")
        print("-" * 120)
    else:
        print(f"{'Package':<30} {'Status':<10} {'Desc':>5}  Best PR")
        print("-" * 110)
    for p in ready:
        author = p["best_pr_author"]
        note = ""
        if author != policy.BOT_AUTHOR:
            note = f" [{author}{'  v1' if p['best_pr_is_v1'] else ''}]"
        if p.get("is_cuda_wontfix"):
            note += " [CUDA WONTFIX]"
        if with_depth:
            depth_str = str(p["depth_to_ci_setup"]) if p["depth_to_ci_setup"] is not None else "?"
            print(f"{p['name']:<30} {p['pr_status']:<10} {depth_str:>5} {p['num_descendants']:>5}  {p['best_pr_url']}{note}")
        else:
            print(f"{p['name']:<30} {p['pr_status']:<10} {p['num_descendants']:>5}  {p['best_pr_url']}{note}")


if __name__ == "__main__":
    main()
