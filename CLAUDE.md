# conda-forge riscv64 migration bot

Tools and workflows for tracking and driving the linux-riscv64 migration on conda-forge.

## Quick start

```bash
cd /home/luhenry/git/conda-forge/.bot
```

### Check current migration status

```bash
python3 riscv64_status.py          # full analysis (queries GitHub for best PRs)
python3 riscv64_status.py --no-gh  # fast, uses migration JSON only
```

### Run a full triage

Analyzes every ready feedstock (in-PR, all deps done), classifies each PR, writes state files, commits.

Tell Claude:
> Run the triage workflow

Claude will invoke `workflows/triage_migration.js` and summarize results.

### Check a single feedstock

Tell Claude:
> Analyze <feedstock-name>

Claude will run `workflows/analyze_feedstock.js` for that feedstock and report its recommendation.

---

## Workflow architecture

```
triage_migration.js          — top-level, runs once per cron tick
  └─ analyze_feedstock.js    — per-feedstock, 4 sequential agents
       ├─ pr-state            fetch PR metadata + CI checks
       ├─ comments            fetch PR comments (best PR + bot PR if separate)
       ├─ ci-analysis         fetch failing CI logs, classify root cause
       └─ recommend           synthesize into one action
```

All workflows are **read-only** (Phase 1+2 only). Phase 3 (act) is not yet implemented.

## Cron

Triage runs automatically at 01:07, 09:07, 17:07 local time (every 8h).
Cron job id: `4b9a95d4`. Auto-expires after 7 days — recreate with:
> Schedule the triage cron every 8 hours

## State files

`state/<feedstock>.json` — written after every triage run, committed to git.

```json
{
  "feedstock": "libffi",
  "best_pr_url": "https://github.com/conda-forge/libffi-feedstock/pull/63",
  "last_checked": "2026-08-21T07:30:00Z",
  "last_action": "NUDGE_MERGE",
  "last_action_at": "2026-08-21T07:30:00Z",
  "confidence": "high",
  "reason": "All CI checks passing including linux_riscv64_",
  "riscv64_ci_passing": true,
  "num_descendants": 148
}
```

## Actions (Phase 1+2 — read-only)

| Action | Meaning |
|---|---|
| `NUDGE_MERGE` | riscv64 CI green, PR not draft, no recent nudge — ready to merge |
| `WAIT_MISSING_DEP` | CI fails because a dep isn't yet available for riscv64 |
| `WAIT_UNRELATED_FAILURE` | Only non-riscv64 platforms fail; riscv64 migration is fine |
| `NEEDS_FIX` | riscv64 CI fails due to a code/build bug |
| `ESCALATE` | Unclear, conflicting PRs, or needs human judgment |
| `SKIP_ALREADY_HANDLED` | Checked < 2h ago with no change |

## PR selection heuristics (`riscv64_status.py`)

For each feedstock the bot PR is the baseline. For unstable bot PRs, open PRs are
scored to find a better alternative:

1. **Non-bot, riscv64-focused, no v1 migration** → score 100 (+20 if supersedes bot)
2. **Bot PR** → score 20 (preferred over v1-only alternatives)
3. **Non-bot, v1 migration + riscv64** → score 10 (+5 if supersedes bot)

The feedstock's current recipe format is checked first — if it already uses
`recipe.yaml`, adding one is not a v1 migration.

## Helpers

`fetch_ci_log.py <url> [--lines N]` — fetch tail of a CI job log.
Supports GitHub Actions and Azure Pipelines URLs.

```bash
python3 fetch_ci_log.py "https://github.com/.../actions/runs/.../job/..." --lines 40
python3 fetch_ci_log.py "https://dev.azure.com/conda-forge/...?buildId=..." --lines 40
```
