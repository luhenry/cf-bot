"""
cf_core.cli — the single seam the JS workflows call through for every deterministic operation.

Exposed as `python3 -m cf_core <verb> ...` (run from the cf-bot repo root, exactly like
`riscv64_status.py` is invoked today). Every subcommand prints one JSON object to stdout, so a
low-effort "relay" agent() call in the JS workflow layer can run the exact command and paste its
stdout back verbatim as the agent's structured return value -- no reasoning required. See
CLAUDE.md's "Workflow architecture" section for how the JS side uses this.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from cf_core import conda_forge_yml_check as cfy
from cf_core import gh_client
from cf_core import graph as g
from cf_core import migration_source as ms
from cf_core import policy
from cf_core import reconciler
from cf_core import snapshot as snap
from cf_core import state_io as sio


def _print(obj) -> None:
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── graph ─────────────────────────────────────────────────────────────────────────────────────

def cmd_graph(args):
    data = ms.fetch_migration_json()
    result = g.summarize(data["_feedstock_status"], args.target)
    if args.save_snapshot:
        now_iso = args.now or _now_iso()
        result["snapshot_diff"] = snap.diff_against_last(data)
        snap.save_snapshot(data, now_iso)
    _print(result)


def cmd_ready_list(args):
    """Priority-sorted list of "ready" (in-PR, all deps done) feedstocks -- ported from
    riscv64_status.py's analyze(), now backed by the single cf_core.graph implementation instead
    of a hand-rolled dict/BFS."""
    data = ms.fetch_migration_json()
    feedstocks = data["_feedstock_status"]
    done_set = set(data.get("done", []))
    in_pr_set = set(data.get("in-pr", []))

    G = g.build_graph(feedstocks)
    depths = g.depth_to_target(G, policy.CI_SETUP_TARGET)

    ready = []
    for name in sorted(in_pr_set):
        if name not in feedstocks:
            continue
        deps = g.parents_of(G, name)
        undone_deps = [d for d in deps if d not in done_set]
        if undone_deps:
            continue
        info = feedstocks[name]
        prior = sio.read_state(name) or {}
        ready.append({
            "name": name,
            "bot_pr_url": info.get("pr_url", ""),
            "pr_status": info.get("pr_status", ""),
            "num_descendants": info.get("num_descendants", 0),
            "depth_to_ci_setup": depths.get(name),
            "is_cuda_wontfix": policy.is_cuda_wontfix(name),
            # Merged in deterministically here instead of an LLM agent `cat`-ing ~70 state files
            # itself, which is what the fetch-list step used to do.
            "last_action": prior.get("last_action"),
            "last_action_at": prior.get("last_action_at"),
            "last_checked": prior.get("last_checked"),
        })

    ready.sort(key=lambda x: (
        x["depth_to_ci_setup"] if x["depth_to_ci_setup"] is not None else float("inf"),
        -x["num_descendants"],
    ))
    _print({"ready": ready, "total_feedstocks": len(feedstocks)})


# ── policy ────────────────────────────────────────────────────────────────────────────────────

def cmd_policy_check_cuda(args):
    _print({"feedstock": args.name, "is_cuda_wontfix": policy.is_cuda_wontfix(args.name)})


def cmd_policy_check_v1_bundle(args):
    files = args.files.split(",") if args.files else []
    _print({"pr_is_v1_migration": policy.pr_is_v1_migration(args.title, files, args.repo_is_v1)})


def cmd_policy_check_commit_message(args):
    message = args.message if args.message is not None else sys.stdin.read()
    _print(policy.check_commit_message(message))


# ── verify ────────────────────────────────────────────────────────────────────────────────────

def cmd_verify_feedstock(args):
    """One-shot deterministic pre-check for a feedstock, run before any LLM judgment in
    analyze_feedstock.js: CUDA-wontfix, the authoritative conda-forge.yml check, and (if PR
    context is supplied) v1-bundle detection + PR scoring."""
    result = {
        "feedstock": args.name,
        "is_cuda_wontfix": policy.is_cuda_wontfix(args.name),
        "conda_forge_yml": cfy.check_feedstock(args.name),
    }

    if args.pr_json:
        pr = json.loads(args.pr_json)
        files = pr.get("files", [])
        repo_is_v1 = pr.get("repo_is_v1")
        if repo_is_v1 is None:
            # Not supplied by the caller -- determine it ourselves via `gh` rather than making
            # the JS side responsible for a second gh call just to answer this one boolean.
            repo_is_v1 = gh_client.repo_uses_v1_recipe(f"conda-forge/{args.name}-feedstock")
        result["repo_is_v1"] = repo_is_v1
        result["pr_is_v1_migration"] = policy.pr_is_v1_migration(pr.get("title", ""), files, repo_is_v1)
        if "author_login" in pr and "bot_pr_number" in pr:
            result["pr_score"] = policy.score_pr(
                author_login=pr["author_login"],
                title=pr.get("title", ""),
                body=pr.get("body"),
                changed_files=files,
                bot_pr_number=pr["bot_pr_number"],
                repo_already_uses_v1=repo_is_v1,
            )

    _print(result)


def cmd_verify_diff_pr(args):
    diff_text = cfy.diff_pr(args.name, args.pr_number)
    _print({"feedstock": args.name, "pr_number": args.pr_number, "diff": diff_text})


# ── reconcile ─────────────────────────────────────────────────────────────────────────────────

def cmd_reconcile(args):
    now_iso = args.now or _now_iso()
    output = {"now": now_iso, "tracked": reconciler.reconcile_tracked(now_iso)}
    if args.ready_names:
        output["ready_already_done"] = reconciler.check_ready_already_done(args.ready_names.split(","))
    _print(output)


# ── state ─────────────────────────────────────────────────────────────────────────────────────

def cmd_state_read(args):
    _print((sio.read_tracked(args.name) if args.tracked else sio.read_state(args.name)) or {})


def cmd_state_write(args):
    fields = json.loads(args.json) if args.json is not None else json.loads(sys.stdin.read())
    result = sio.write_tracked(args.name, fields) if args.tracked else sio.write_state(args.name, fields)
    _print(result)


# ── snapshot ──────────────────────────────────────────────────────────────────────────────────

def cmd_snapshot_diff(args):
    data = ms.fetch_migration_json()
    _print(snap.diff_against_last(data))


# ── argparse wiring ───────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cf_core")
    sub = parser.add_subparsers(dest="command", required=True)

    p_graph = sub.add_parser("graph", help="dependency-graph summary for a target")
    p_graph.add_argument("--target", default=policy.CI_SETUP_TARGET)
    p_graph.add_argument("--save-snapshot", action="store_true")
    p_graph.add_argument("--now")
    p_graph.set_defaults(func=cmd_graph)

    p_ready = sub.add_parser("ready-list", help="priority-sorted ready-feedstock list")
    p_ready.set_defaults(func=cmd_ready_list)

    p_policy = sub.add_parser("policy", help="policy predicate checks")
    policy_sub = p_policy.add_subparsers(dest="policy_command", required=True)

    p_cuda = policy_sub.add_parser("check-cuda")
    p_cuda.add_argument("name")
    p_cuda.set_defaults(func=cmd_policy_check_cuda)

    p_v1 = policy_sub.add_parser("check-v1-bundle")
    p_v1.add_argument("--title", required=True)
    p_v1.add_argument("--files", default="", help="comma-separated changed file paths")
    p_v1.add_argument("--repo-is-v1", action="store_true")
    p_v1.set_defaults(func=cmd_policy_check_v1_bundle)

    p_commit = policy_sub.add_parser("check-commit-message")
    p_commit.add_argument("--message", help="reads stdin if omitted")
    p_commit.set_defaults(func=cmd_policy_check_commit_message)

    p_verify = sub.add_parser("verify", help="deterministic per-feedstock pre-checks")
    verify_sub = p_verify.add_subparsers(dest="verify_command", required=True)

    p_verify_fs = verify_sub.add_parser("feedstock")
    p_verify_fs.add_argument("name")
    p_verify_fs.add_argument("--pr-json", help="compact JSON: title, files, body, author_login, bot_pr_number, repo_is_v1")
    p_verify_fs.set_defaults(func=cmd_verify_feedstock)

    p_verify_diff = verify_sub.add_parser("diff-pr")
    p_verify_diff.add_argument("name")
    p_verify_diff.add_argument("pr_number", type=int)
    p_verify_diff.set_defaults(func=cmd_verify_diff_pr)

    p_reconcile = sub.add_parser("reconcile", help="re-verify existing state against ground truth")
    p_reconcile.add_argument("--now")
    p_reconcile.add_argument("--ready-names", help="comma-separated list of ready feedstock names to also check")
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_state = sub.add_parser("state", help="deterministic state file read/write")
    state_sub = p_state.add_subparsers(dest="state_command", required=True)

    p_state_read = state_sub.add_parser("read")
    p_state_read.add_argument("name")
    p_state_read.add_argument("--tracked", action="store_true")
    p_state_read.set_defaults(func=cmd_state_read)

    p_state_write = state_sub.add_parser("write")
    p_state_write.add_argument("name")
    p_state_write.add_argument("--tracked", action="store_true")
    p_state_write.add_argument("--json", help="compact JSON fields to merge; reads stdin if omitted")
    p_state_write.set_defaults(func=cmd_state_write)

    p_snapshot = sub.add_parser("snapshot", help="migration-graph snapshot diffing")
    snapshot_sub = p_snapshot.add_subparsers(dest="snapshot_command", required=True)
    p_snap_diff = snapshot_sub.add_parser("diff")
    p_snap_diff.set_defaults(func=cmd_snapshot_diff)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
