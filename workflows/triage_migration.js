export const meta = {
  name: 'triage-migration',
  description: 'Top-level: analyze all ready riscv64 feedstocks and write state files (read-only)',
  phases: [
    { title: 'Fetch', detail: 'get migration status and ready feedstock list' },
    { title: 'Analyze', detail: 'run per-feedstock analysis in parallel' },
    { title: 'Summarize', detail: 'write state files and produce run summary' },
  ],
}

// ── Phase 1: fetch ready feedstocks ──────────────────────────────────────────

phase('Fetch')

// Use the Python script to get the full list including best_pr selection via gh.
// This is cheaper than having each sub-agent discover it independently.
const fetchResult = await agent(`
Run the migration status script and return structured data about every ready feedstock.

  cd /home/luhenry/git/conda-forge/.bot && python3 riscv64_status.py 2>/dev/null

Then for each feedstock in the output, also read its state file if it exists:
  cat /home/luhenry/git/conda-forge/.bot/state/<feedstock>.json 2>/dev/null

Return a JSON object with a single key "items" whose value is an array.
Each element:
{
  "name": "feedstock-name",
  "bot_pr_url": "https://...",
  "best_pr_url": "https://...",
  "best_pr_author": "username",
  "pr_status": "clean"|"unstable",
  "num_descendants": 42,
  "last_action": "ACTION_NAME or null",
  "last_action_at": "ISO timestamp or null",
  "last_checked": "ISO timestamp or null"
}
`, { label: 'fetch-list', phase: 'Fetch', schema: {
  type: 'object',
  required: ['items'],
  properties: {
    items: { type: 'array', items: { type: 'object' } }
  }
}})

const feedstockList = fetchResult.items

// Filter: skip feedstocks checked < 2h ago with no change, unless they need attention
const now = new Date().toISOString()
const toAnalyze = feedstockList.filter(f => {
  if (!f.last_checked) return true
  const age = Date.now() - new Date(f.last_checked).getTime()
  const twoHours = 2 * 60 * 60 * 1000
  if (age < twoHours && f.last_action === 'SKIP_ALREADY_HANDLED') return false
  return true
})

log(`${feedstockList.length} ready feedstocks, ${toAnalyze.length} to analyze this run`)

// ── Phase 2: per-feedstock analysis ──────────────────────────────────────────

phase('Analyze')

const ANALYZE_SCRIPT = '/home/luhenry/git/conda-forge/.bot/workflows/analyze_feedstock.js'

const results = await pipeline(
  toAnalyze,
  f => workflow({ scriptPath: ANALYZE_SCRIPT }, f),
)

// ── Phase 3: write state files and summarize ─────────────────────────────────

phase('Summarize')

await agent(`
Write state files and a summary for this migration triage run.

Run timestamp: ${now}

Results (one per feedstock):
${JSON.stringify(results.filter(Boolean), null, 2)}

For each feedstock result:
1. Write (overwrite) /home/luhenry/git/conda-forge/.bot/state/<name>.json with:
   {
     "feedstock": "<name>",
     "best_pr_url": "<url>",
     "last_checked": "${now}",
     "last_action": "<result.recommendation.action>",
     "last_action_at": "${now}",
     "confidence": "<result.recommendation.confidence>",
     "reason": "<result.recommendation.reason>",
     "riscv64_ci_passing": <bool from result.recommendation.riscv64_ci_passing>,
     "num_descendants": <int>
   }

2. Stage and commit all state files:
   git -C /home/luhenry/git/conda-forge/.bot add state/
   git -C /home/luhenry/git/conda-forge/.bot commit -m "triage ${now.slice(0,16)}: ${toAnalyze.length} feedstocks"

3. Print a human-readable summary grouped into sections:

   ## Needs attention
   (action is NUDGE_MERGE, NEEDS_FIX, or ESCALATE — sorted by num_descendants desc)

   ## Waiting
   (action is WAIT_MISSING_DEP or WAIT_UNRELATED_FAILURE)

   ## Skipped
   (action is SKIP_ALREADY_HANDLED)

   Format: one line per feedstock: <name> | <num_descendants> deps | <action> | <reason>
`, { label: 'summarize', phase: 'Summarize' })

return results.filter(Boolean)
