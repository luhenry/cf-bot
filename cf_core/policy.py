"""
cf_core.policy — single source of truth for every riscv64-migration policy rule.

Before this module existed, three of these rules were each defined independently in two
places (a Python regex in riscv64_status.py and a separate JS regex literal in
analyze_feedstock.js for the CUDA pattern; a regex in Python but a full English restatement
inside an LLM prompt for the v1-bundle rule), with nothing enforcing that they agreed with each
other. Every predicate here is called from both riscv64_status.py and (via `cf_core verify` /
`cf_core policy` CLI relay calls) the JS workflows — there is now exactly one implementation of
each rule, not two-going-on-diverged.

See CLAUDE.md for the human-readable explanation of each policy; this module is what actually
enforces it.
"""
from __future__ import annotations

import re
from typing import Optional

# ── CUDA / GPU-vendor platform wontfix ───────────────────────────────────────────────────────
# RISC-V has no NVIDIA CUDA toolkit. Feedstocks matching this pattern fundamentally require CUDA
# (not just optionally depend on it -- mpich/openmpi already had that dependency dropped for
# riscv64 and are intentionally NOT matched here) and are permanently platform-blocked, not a
# fixable work item.
CUDA_WONTFIX_RE = re.compile(r"^(cuda-|libcu|nccl)", re.IGNORECASE)


def is_cuda_wontfix(feedstock_name: str) -> bool:
    return bool(CUDA_WONTFIX_RE.match(feedstock_name))


# ── Never bundle v1/rattler-build migration with riscv64 ────────────────────────────────────
# Known cross-compiled-testing issue under the v1/rattler-build recipe format. riscv64 migration
# and v1 migration must never land in the same PR -- see CLAUDE.md "Policy: never bundle v1
# migration into the riscv64 path" (reference case: perl-feedstock#76 vs #77).
V1_TITLE_KEYWORDS = ("v1", "rattler", "pixi")


def pr_is_v1_migration(pr_title: str, changed_files: list[str], repo_already_uses_v1: bool) -> bool:
    """True if this PR *introduces* the v1/rattler-build recipe format (not just uses an
    already-migrated repo's existing format -- if the repo's main branch already has
    recipe/recipe.yaml, nothing in this PR is "migrating" anything)."""
    if repo_already_uses_v1:
        return False
    has_recipe_yaml = any("recipe.yaml" in f for f in changed_files)
    title_v1 = any(kw in pr_title.lower() for kw in V1_TITLE_KEYWORDS)
    return has_recipe_yaml or title_v1


# ── PR selection scoring ─────────────────────────────────────────────────────────────────────
# Higher score = better candidate to focus on. See CLAUDE.md "PR selection heuristics".
PR_SCORE_BOT = 20
PR_SCORE_FOCUSED = 100
PR_SCORE_FOCUSED_SUPERSEDE_BONUS = 20
PR_SCORE_V1_BUNDLED = 10
PR_SCORE_V1_BUNDLED_SUPERSEDE_BONUS = 5

BOT_AUTHOR = "regro-cf-autotick-bot"
RISCV_TITLE_KEYWORDS = ("riscv64", "riscv")

_SUPERSEDES_RE = re.compile(r"[Ss]upersedes[^#\n]*#(\d+)")


def pr_supersedes_bot(pr_body: Optional[str], bot_pr_number: int) -> bool:
    matches = _SUPERSEDES_RE.findall(pr_body or "")
    return str(bot_pr_number) in matches


def score_pr(
    *,
    author_login: str,
    title: str,
    body: Optional[str],
    changed_files: list[str],
    bot_pr_number: int,
    repo_already_uses_v1: bool,
) -> int:
    """0 = irrelevant/ignore. See module docstring's PR_SCORE_* constants for tiers."""
    is_bot = author_login == BOT_AUTHOR
    is_riscv = any(kw in title.lower() for kw in RISCV_TITLE_KEYWORDS)

    if is_bot:
        return PR_SCORE_BOT
    if not is_riscv:
        return 0

    if pr_is_v1_migration(title, changed_files, repo_already_uses_v1):
        score = PR_SCORE_V1_BUNDLED
        if pr_supersedes_bot(body, bot_pr_number):
            score += PR_SCORE_V1_BUNDLED_SUPERSEDE_BONUS
        return score

    score = PR_SCORE_FOCUSED
    if pr_supersedes_bot(body, bot_pr_number):
        score += PR_SCORE_FOCUSED_SUPERSEDE_BONUS
    return score


# ── Priority target ──────────────────────────────────────────────────────────────────────────
# conda-forge-ci-setup is the #1 priority target: it's the CI tooling infrastructure that
# (transitively) gates proper linting/rerendering/testing for every other feedstock on riscv64.
CI_SETUP_TARGET = "conda-forge-ci-setup"


# ── Action-code taxonomy ─────────────────────────────────────────────────────────────────────
# state/<feedstock>.json's `last_action`. This is the full, enforced set -- state_io.py refuses
# to WRITE anything outside it (existing files with an older/unknown value are still readable;
# nothing here breaks backward compatibility of on-disk data).
ACTION_CODES = frozenset({
    "NUDGE_MERGE",              # riscv64 CI green, PR not draft, no recent nudge -- ready to merge
    "WAIT_MISSING_DEP",         # CI fails because a dep isn't yet available for riscv64
    "WAIT_UNRELATED_FAILURE",   # only non-riscv64 platforms fail; riscv64 migration is fine
    "NEEDS_FIX",                # riscv64 CI fails due to a code/build bug
    "NEEDS_MINIMAL_PR",         # only an unwanted PR exists (e.g. bundles a v1 migration)
    "WONTFIX_PLATFORM",         # structurally incompatible with riscv64 (e.g. requires CUDA)
    "WAIT_UPSTREAM",            # decided direction, blocked on an upstream project shipping riscv64 support
    "ESCALATE",                 # genuinely unclear, needs human judgment
    "SKIP_ALREADY_HANDLED",     # checked recently, no change
})

# state/_tracked/<name>.json's `last_action` -- shadow dependencies not on the official migration
# page. Superset of ACTION_CODES (a shadow dep can reach any of the same terminal states) plus
# states specific to "this isn't even on our radar via an official PR yet".
TRACKED_ACTION_CODES = ACTION_CODES | frozenset({
    "WAIT_NOT_STARTED",         # no riscv64 PR open yet, migration hasn't started for this dep
    "NEEDS_VERIFICATION",       # status genuinely unclear from available signals, needs a check
    "DONE",                     # confirmed riscv64 support already merged (via authoritative check)
})


# ── Commit attribution guard ─────────────────────────────────────────────────────────────────
# Non-negotiable, per the human operator: no commit or PR may ever mention Claude. This is a
# deterministic pre-commit safety net, not just documentation -- see CLAUDE.md "Attribution rule".
BANNED_COMMIT_TERMS = (
    "claude",
    "anthropic",
    "co-authored-by: claude",
    "generated with claude",
    "🤖 generated",
)


def check_commit_message(message: str) -> dict:
    """Case-insensitive scan for banned attribution terms. Returns
    {"clean": bool, "violations": [term, ...]} -- callers must refuse to commit if not clean."""
    lower = message.lower()
    violations = [term for term in BANNED_COMMIT_TERMS if term in lower]
    return {"clean": not violations, "violations": violations}
