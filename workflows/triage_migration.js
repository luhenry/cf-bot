export const meta = {
  name: 'triage-migration',
  description: 'Top-level: analyze all ready riscv64 feedstocks and write state files (read-only)',
  phases: [
    { title: 'Fetch', detail: 'get migration status and ready feedstock list' },
    { title: 'Analyze', detail: 'analyze each feedstock sequentially' },
    { title: 'Summarize', detail: 'write state files and produce run summary' },
  ],
}

// args: { now: "ISO timestamp" }
const now = args?.now ?? '1970-01-01T00:00:00Z'

// ── Phase 1: fetch ready feedstocks ──────────────────────────────────────────

phase('Fetch')

const fetchResult = await agent(`
Run the migration status script and return structured data about every ready feedstock.

  cd /home/luhenry/git/conda-forge/.bot && python3 riscv64_status.py 2>/dev/null

Then for each feedstock in the output also read its state file if it exists:
  cat /home/luhenry/git/conda-forge/.bot/state/<feedstock>.json 2>/dev/null

Return a JSON object:
{
  "items": [
    {
      "name": "feedstock-name",
      "bot_pr_url": "https://...",
      "best_pr_url": "https://...",
      "best_pr_author": "username",
      "pr_status": "clean",
      "num_descendants": 42,
      "last_action": null,
      "last_action_at": null,
      "last_checked": null
    }
  ]
}
`, { label: 'fetch-list', phase: 'Fetch', schema: {
  type: 'object',
  required: ['items'],
  properties: { items: { type: 'array', items: { type: 'object' } } }
}})

const feedstockList = fetchResult.items

const toAnalyze = feedstockList.filter(f => {
  if (!f.last_checked) return true
  const age = new Date(now).getTime() - new Date(f.last_checked).getTime()
  const twoHours = 2 * 60 * 60 * 1000
  if (age < twoHours && f.last_action === 'SKIP_ALREADY_HANDLED') return false
  return true
})

log(`${feedstockList.length} ready feedstocks, ${toAnalyze.length} to analyze this run`)

// ── Phase 2: per-feedstock analysis (sequential) ─────────────────────────────

phase('Analyze')

const ANALYZE_SCRIPT = '/home/luhenry/git/conda-forge/.bot/workflows/analyze_feedstock.js'

const results = []
for (const f of toAnalyze) {
  log(`analyzing ${f.name} (${f.num_descendants} descendants)`)
  const r = await workflow({ scriptPath: ANALYZE_SCRIPT }, f)
  if (r) results.push(r)
}

// ── Phase 3: write state files and summarize ─────────────────────────────────

phase('Summarize')

await agent(`
Write state files and a summary for this migration triage run.

Run timestamp: ${now}
Feedstocks analyzed: ${results.length}

Results:
${JSON.stringify(results, null, 2)}

For each result:
1. Write /home/luhenry/git/conda-forge/.bot/state/<feedstock>.json:
   {
     "feedstock": "<name>",
     "best_pr_url": "<url>",
     "last_checked": "${now}",
     "last_action": "<result.recommendation.action>",
     "last_action_at": "${now}",
     "confidence": "<result.recommendation.confidence>",
     "reason": "<result.recommendation.reason>",
     "riscv64_ci_passing": <bool>,
     "num_descendants": <int>
   }

2. Commit:
   git -C /home/luhenry/git/conda-forge/.bot add state/
   git -C /home/luhenry/git/conda-forge/.bot commit -m "triage ${now.slice(0,16)}: ${results.length} feedstocks"

3. Print summary grouped into sections:

## Needs attention
(NUDGE_MERGE, NEEDS_FIX, ESCALATE — sorted by num_descendants desc)
<name> | <num_descendants> deps | <action> | <reason>

## Waiting
(WAIT_MISSING_DEP, WAIT_UNRELATED_FAILURE)
<name> | <num_descendants> deps | <action> | <reason>

## Skipped
(SKIP_ALREADY_HANDLED)
<name> | <action>
`, { label: 'summarize', phase: 'Summarize' })

return results
