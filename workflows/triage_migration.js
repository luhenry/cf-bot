export const meta = {
  name: 'triage-migration',
  description: 'Top-level: reconcile existing state against ground truth, analyze ready riscv64 feedstocks, write state files (read-only)',
  phases: [
    { title: 'Fetch', detail: 'ready-feedstock list (already-sorted, already-structured JSON from cf_core) + migration-graph snapshot diff' },
    { title: 'Reconcile', detail: 'deterministically re-verify existing shadow deps + already-ready feedstocks against conda-forge.yml before spending any LLM cycles' },
    { title: 'Analyze', detail: 'per-feedstock, pipelined (no cross-item barrier -- one slow feedstock no longer blocks the rest)' },
    { title: 'Shadow', detail: 'discover new off-page blockers (fuzzy) -- reconciling EXISTING ones already happened in Reconcile' },
    { title: 'Summarize', detail: 'deterministic state writes, commit-message guard, run summary' },
  ],
}

// args: { now: "ISO timestamp" }
const now = args?.now ?? '1970-01-01T00:00:00Z'
const CF_BOT_DIR = '/home/luhenry/git/conda-forge/.bot'
const ANALYZE_SCRIPT = `${CF_BOT_DIR}/workflows/analyze_feedstock.js`

// ── Phase 1: fetch ready feedstocks + snapshot diff ──────────────────────────────────────────
//
// Before this rearchitecture, this phase asked an LLM agent to run riscv64_status.py, parse its
// printed TABLE OUTPUT back into JSON, and separately `cat` every existing state/*.json file by
// hand to attach prior last_action/last_checked. `cf_core ready-list` now returns already-
// structured, already-prior-state-merged JSON directly -- the agent here is a pure relay, doing
// no parsing or reasoning at all.

phase('Fetch')

const [fetchResult, snapshotResult] = await parallel([
  () => agent(`
Run exactly this command and return its stdout JSON verbatim as your answer -- do not parse,
reformat, or interpret it:

  cd ${CF_BOT_DIR} && python3 -m cf_core ready-list
`, { label: 'fetch-list', phase: 'Fetch', effort: 'low', schema: {
    type: 'object',
    required: ['ready', 'total_feedstocks'],
    properties: { ready: { type: 'array', items: { type: 'object' } }, total_feedstocks: { type: 'number' } },
  }}),
  () => agent(`
Run exactly this command and return its stdout JSON verbatim as your answer:

  cd ${CF_BOT_DIR} && python3 -m cf_core graph --target conda-forge-ci-setup --save-snapshot --now "${now}"
`, { label: 'snapshot', phase: 'Fetch', effort: 'low', schema: {
    type: 'object',
    properties: { snapshot_diff: { type: 'object' } },
  }}),
])

const feedstockList = fetchResult.ready
// Already sorted by cf_core (depth_to_ci_setup asc, num_descendants desc) -- no JS-side sort needed.

const diff = snapshotResult?.snapshot_diff
if (diff?.has_previous) {
  log(`since last run: ${diff.newly_done.length} newly done, ${diff.newly_in_pr.length} newly in-pr`)
  if (diff.dropped_from_in_pr.length > 0) {
    log(`dropped from in-pr without a matching "done" entry (likely merged, worth a glance): ${diff.dropped_from_in_pr.join(', ')}`)
  }
}

// ── Phase 2: reconcile EXISTING state against ground truth, before spending any LLM cycles ──
//
// Before this rearchitecture, state/_tracked/*.json entries were only ever updated reactively --
// when THAT run's LLM agents happened to mention the name in a comment/log. Existing entries
// were never re-checked on their own; state/_tracked/zstd.json sat at NEEDS_VERIFICATION long
// after zstd had actually shipped riscv64, because nothing in the pipeline re-verified it. This
// phase runs FIRST (not as an afterthought at the end) specifically so its findings -- which
// "ready" feedstocks are already actually done -- can be used to skip wasted Analyze-phase work.

phase('Reconcile')

const reconcileResult = await agent(`
Run exactly this command and return its stdout JSON verbatim as your answer:

  cd ${CF_BOT_DIR} && python3 -m cf_core reconcile --now "${now}" --ready-names "${feedstockList.map(f => f.name).join(',')}"
`, { label: 'reconcile', phase: 'Reconcile', effort: 'low', schema: {
  type: 'object',
  properties: {
    tracked: { type: 'array' },
    ready_already_done: { type: 'array' },
  },
}})

const trackedChanges = (reconcileResult.tracked ?? []).filter(r => r.changed)
if (trackedChanges.length > 0) {
  log(`reconciled ${trackedChanges.length} shadow-dependency status change(s): ${trackedChanges.map(c => `${c.name} -> ${c.new_action}`).join(', ')}`)
}

const alreadyDoneNames = new Set(reconcileResult.ready_already_done ?? [])
if (alreadyDoneNames.size > 0) {
  log(`${alreadyDoneNames.size} "ready" feedstock(s) are actually already done per conda-forge.yml (migration-page lag): ${[...alreadyDoneNames].join(', ')} -- skipping analysis`)
}

// ── Phase 3: per-feedstock analysis, pipelined ───────────────────────────────────────────────
//
// Before this rearchitecture, this was a strict sequential `for` loop -- one feedstock's full
// 4-stage chain had to finish before the next one started, even though every feedstock's chain
// is fully independent of every other's. Switched to pipeline(): each feedstock runs its stages
// independently, no cross-item barrier, so wall-clock is bounded by the slowest single chain
// rather than the sum of all of them.

phase('Analyze')

const toAnalyze = feedstockList.filter(f => {
  if (alreadyDoneNames.has(f.name)) return false
  if (!f.last_checked) return true
  const age = new Date(now).getTime() - new Date(f.last_checked).getTime()
  const twoHours = 2 * 60 * 60 * 1000
  if (age < twoHours && f.last_action === 'SKIP_ALREADY_HANDLED') return false
  return true
})

log(`${feedstockList.length} ready feedstocks, ${alreadyDoneNames.size} already done (reconciled), ${toAnalyze.length} to analyze this run`)

const analyzeResults = await pipeline(
  toAnalyze,
  f => {
    log(`analyzing ${f.name} (${f.num_descendants} descendants, depth ${f.depth_to_ci_setup ?? '?'})`)
    return workflow({ scriptPath: ANALYZE_SCRIPT }, f)
  },
)

const results = analyzeResults.filter(Boolean)

// ── Phase 4: shadow-dependency DISCOVERY (fuzzy) ─────────────────────────────────────────────
//
// Reconciling EXISTING shadow entries against ground truth already happened deterministically
// in the Reconcile phase above. What's left here is genuinely fuzzy: spotting a BRAND-NEW
// "depends on X" mention that hasn't been tracked before at all.

phase('Shadow')

const onPageNames = new Set(feedstockList.map(f => f.name))
const alreadyTrackedNames = new Set(
  (reconcileResult.tracked ?? []).map(r => r.name)
)
const shadowMentions = new Map() // name -> Set(discovered_from)

for (const r of results) {
  for (const dep of (r.blocking_dependency_feedstocks ?? [])) {
    if (onPageNames.has(dep) || dep === r.feedstock) continue
    if (!shadowMentions.has(dep)) shadowMentions.set(dep, new Set())
    shadowMentions.get(dep).add(r.feedstock)
  }
}

const newShadowNames = [...shadowMentions.keys()].filter(name => !alreadyTrackedNames.has(name))
if (newShadowNames.length > 0) {
  log(`${newShadowNames.length} NEW shadow dependency(ies) discovered this run: ${newShadowNames.join(', ')}`)
}

for (const dep of newShadowNames) {
  const discoveredFrom = [...shadowMentions.get(dep)]
  await agent(`
A new shadow dependency "${dep}" was just discovered (mentioned as a blocker in a PR comment or
CI log for: ${discoveredFrom.join(', ')}), but it isn't tracked yet. Gather what's known about it:

1. Check whether a riscv64 PR already exists for it:
   gh pr list --repo conda-forge/${dep}-feedstock --state all --search "riscv64 in:title" \\
     --json number,title,author,state,isDraft,url --limit 5

2. Run this deterministic check and note its result:
   cd ${CF_BOT_DIR} && python3 -m cf_core verify feedstock ${dep}

3. Then write the initial tracking record via cf_core (do NOT use the Write tool directly --
   this keeps schema validation and additive-merge semantics in one place):

   cd ${CF_BOT_DIR} && python3 -m cf_core state write ${dep} --tracked --json '<compact JSON>'

   Fields to include: discovered_from (${JSON.stringify(discoveredFrom)}), first_seen ("${now}"),
   last_checked ("${now}"), riscv64_pr_url (most relevant open PR from step 1, or null),
   riscv64_pr_status (one-line note, e.g. "no PR yet", "draft", "open, v1-bundled", "open, minimal"),
   last_action: DONE if step 2's conda_forge_yml.has_riscv64 is true, WONTFIX_PLATFORM if step 2's
   is_cuda_wontfix is true, otherwise NEEDS_MINIMAL_PR | WAIT_NOT_STARTED | NUDGE_MERGE | NEEDS_FIX
   based on what step 1 found. Run the write command yourself with the JSON filled in.
`, { label: `shadow-discover:${dep}`, phase: 'Shadow' })
}

// ── Phase 5: write state files, guard the commit, summarize ─────────────────────────────────

phase('Summarize')

// Deterministic state writes -- before this rearchitecture, an LLM agent was asked to compose a
// "Write tool directly" call with the JSON embedded in its own prompt for every single result, a
// mechanical operation with zero judgment content sitting on the LLM critical path for no
// benefit. Each write below is a plain relay call with no reasoning required.

for (const r of results) {
  const rec = r.recommendation ?? {}
  const src = feedstockList.find(f => f.name === r.feedstock)
  const fields = {
    best_pr_url: r.best_pr_url,
    last_checked: now,
    last_action: rec.action ?? null,
    last_action_at: now,
    confidence: rec.confidence ?? null,
    reason: rec.reason ?? null,
    riscv64_ci_passing: rec.riscv64_ci_passing ?? null,
    num_descendants: r.num_descendants,
    depth_to_ci_setup: src?.depth_to_ci_setup ?? null,
    blocking_dependency_feedstocks: r.blocking_dependency_feedstocks ?? [],
  }
  await agent(`
Run exactly this command (fields already validated/merged by cf_core -- no need to read the
existing file first, the write is additive):

  cd ${CF_BOT_DIR} && python3 -m cf_core state write ${r.feedstock} --json '${JSON.stringify(fields).replace(/'/g, "'\\''")}'
`, { label: `write-state:${r.feedstock}`, phase: 'Summarize', effort: 'low' })
}

// Commit-message attribution guard -- deterministic, before this rearchitecture the "never
// mention Claude" rule (the single most emphatically stated constraint in this whole project)
// had zero automated enforcement, only documentation.

const commitMessage = `triage ${now.slice(0, 16)}: ${results.length} feedstocks, ${newShadowNames.length} new shadow deps, ${trackedChanges.length} reconciled`

const commitGuard = await agent(`
Run exactly this command and return its stdout JSON verbatim:

  cd ${CF_BOT_DIR} && python3 -m cf_core policy check-commit-message --message "${commitMessage.replace(/"/g, '\\"')}"
`, { label: 'commit-guard', phase: 'Summarize', effort: 'low', schema: {
  type: 'object',
  required: ['clean'],
  properties: { clean: { type: 'boolean' }, violations: { type: 'array' } },
}})

if (!commitGuard.clean) {
  log(`REFUSING TO COMMIT: commit message failed the attribution guard (violations: ${commitGuard.violations.join(', ')}). This should never happen from a template message -- investigate before retrying.`)
} else {
  await agent(`
Commit all state files (including state/_tracked/ shadow entries and state/_snapshot/) and print
a summary.

Run:
  git -C ${CF_BOT_DIR} add state/
  git -C ${CF_BOT_DIR} commit -m "${commitMessage.replace(/"/g, '\\"')}" || echo "nothing to commit"

Then print a summary of these results grouped into sections:

## Needs attention
(action is NUDGE_MERGE, NEEDS_FIX, NEEDS_MINIMAL_PR, or ESCALATE -- sorted by depth_to_ci_setup asc, then num_descendants desc)

## Waiting
(action is WAIT_MISSING_DEP, WAIT_UNRELATED_FAILURE, or WAIT_UPSTREAM)

## Wontfix (platform)
(action is WONTFIX_PLATFORM)

## Already done (reconciled this run)
(from the Reconcile phase: ${JSON.stringify([...alreadyDoneNames])})

## Skipped
(action is SKIP_ALREADY_HANDLED)

## Shadow dependencies
Reconciled this run: ${JSON.stringify(trackedChanges)}
Newly discovered this run: ${JSON.stringify(newShadowNames)}

Format each "Needs attention"/"Waiting"/"Wontfix"/"Skipped" line as:
<name> | depth <depth_to_ci_setup or ?> | <num_descendants> deps | <action> | <reason>

Results:
${JSON.stringify(results.map(r => ({
    feedstock: r.feedstock,
    num_descendants: r.num_descendants,
    depth_to_ci_setup: feedstockList.find(f => f.name === r.feedstock)?.depth_to_ci_setup ?? null,
    action: r.recommendation?.action,
    reason: r.recommendation?.reason,
  })), null, 2)}
`, { label: 'commit-and-summarize', phase: 'Summarize' })
}

return results
