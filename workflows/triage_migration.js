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

// Write state files directly in the script (no agent needed, avoids failures)
for (const r of results) {
  const rec = r.recommendation ?? {}
  const state = {
    feedstock: r.feedstock,
    best_pr_url: r.best_pr_url,
    last_checked: now,
    last_action: rec.action ?? null,
    last_action_at: now,
    confidence: rec.confidence ?? null,
    reason: rec.reason ?? null,
    riscv64_ci_passing: rec.riscv64_ci_passing ?? null,
    num_descendants: r.num_descendants,
  }
  await agent(`
Write this JSON to /home/luhenry/git/conda-forge/.bot/state/${r.feedstock}.json (overwrite):
${JSON.stringify(state, null, 2)}
Use the Write tool directly.
`, { label: `write-state:${r.feedstock}`, phase: 'Summarize' })
}

await agent(`
Commit all state files and print a summary.

Run:
  git -C /home/luhenry/git/conda-forge/.bot add state/
  git -C /home/luhenry/git/conda-forge/.bot commit -m "triage ${now.slice(0,16)}: ${results.length} feedstocks" || echo "nothing to commit"

Then print a summary of these results grouped into sections:

## Needs attention
(action is NUDGE_MERGE, NEEDS_FIX, or ESCALATE — sorted by num_descendants desc)

## Waiting
(action is WAIT_MISSING_DEP or WAIT_UNRELATED_FAILURE)

## Skipped
(action is SKIP_ALREADY_HANDLED)

Format each line: <name> | <num_descendants> deps | <action> | <reason>

Results:
${JSON.stringify(results.map(r => ({
  feedstock: r.feedstock,
  num_descendants: r.num_descendants,
  action: r.recommendation?.action,
  reason: r.recommendation?.reason,
})), null, 2)}
`, { label: 'commit-and-summarize', phase: 'Summarize' })

return results
