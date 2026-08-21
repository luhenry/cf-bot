export const meta = {
  name: 'analyze-feedstock',
  description: 'Phase 1+2: gather PR state, comments, CI results and decide action (read-only)',
  phases: [
    { title: 'Gather', detail: 'fetch PR state, comments, CI checks sequentially' },
    { title: 'Analyze', detail: 'synthesize findings into a recommended action' },
  ],
}

// args: { name, bot_pr_url, best_pr_url, best_pr_author, pr_status, num_descendants }

const feedstock = args.name
const repo = `conda-forge/${feedstock}-feedstock`
const botPrUrl = args.bot_pr_url
const bestPrUrl = args.best_pr_url
const bestPrAuthor = args.best_pr_author
const migrationStatus = args.pr_status   // "clean" | "unstable"
const bestPrNumber = bestPrUrl.match(/\/pull\/(\d+)/)?.[1]
const botPrNumber  = botPrUrl.match(/\/pull\/(\d+)/)?.[1]
const hasSeparateUserPr = bestPrUrl !== botPrUrl

// ── Phase 1: gather (sequential) ─────────────────────────────────────────────

phase('Gather')

const prState = await agent(`
You are gathering facts about a conda-forge feedstock PR. Do not interpret — only report.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl}

Run these commands and return the results as a JSON object:

1. gh pr view ${bestPrNumber} --repo ${repo} \\
     --json number,title,author,state,isDraft,createdAt,updatedAt,labels,baseRefName

2. gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup \\
   then extract the list of checks, keeping: name, status, conclusion, targetUrl

3. If bot PR is separate (${hasSeparateUserPr}), also fetch:
   gh pr view ${botPrNumber} --repo ${repo} --json number,title,state

Return JSON:
{
  "pr": { "number": 0, "title": "", "author": "", "isDraft": false, "createdAt": "", "updatedAt": "", "labels": [] },
  "checks": [ { "name": "", "status": "", "conclusion": "", "url": "" } ],
  "bot_pr_open": true,
  "bot_pr_checks": []
}
`, { label: 'pr-state', phase: 'Gather', schema: {
  type: 'object',
  required: ['pr', 'checks'],
  properties: {
    pr: { type: 'object' },
    checks: { type: 'array' },
    bot_pr_open: { type: 'boolean' },
    bot_pr_checks: { type: 'array' },
  }
}})

const comments = await agent(`
You are reading comments on a conda-forge feedstock PR. Do not interpret — only report.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl} (number: ${bestPrNumber})

Run:
  gh api repos/${repo}/issues/${bestPrNumber}/comments \\
    --jq '.[] | {author: .user.login, date: .created_at, body: .body}'

For each comment return:
- author, date (ISO), a one-sentence summary
- flags: is_linter, is_rerun_request, is_our_bot (author "luhenry-conda-forge-bot"),
         is_failure_explanation, is_maintainer_question

${hasSeparateUserPr ? `Also fetch bot PR comments (number ${botPrNumber}):
  gh api repos/${repo}/issues/${botPrNumber}/comments \\
    --jq '.[] | {author: .user.login, date: .created_at, body: .body}'
` : ''}

Return JSON:
{
  "best_pr_comments": [ { "author": "", "date": "", "summary": "", "is_linter": false, "is_rerun_request": false, "is_our_bot": false, "is_failure_explanation": false, "is_maintainer_question": false } ],
  "bot_pr_comments": []
}
`, { label: 'comments', phase: 'Gather', schema: {
  type: 'object',
  required: ['best_pr_comments', 'bot_pr_comments'],
  properties: {
    best_pr_comments: { type: 'array' },
    bot_pr_comments:  { type: 'array' },
  }
}})

const ciAnalysis = await agent(`
You are diagnosing CI failures on a conda-forge feedstock PR. Do not suggest fixes — only classify.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl} (number: ${bestPrNumber})
Migration tracker status: ${migrationStatus}

Step 1 — identify failing checks:
  gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup

Look for checks where conclusion == "FAILURE" or status == "FAILURE".

Step 2 — classify failure scope:
  - "riscv64-only": only linux_riscv64_* checks fail
  - "unrelated": failure only on non-riscv64 platforms (win_64_, osx_*, etc.)
  - "all-platforms": failures span riscv64 and other platforms
  - "pending": checks still running
  - "none": all passing

Step 3 — for up to 2 failing checks, fetch the log (prioritize linux_riscv64_* first):
  python3 /home/luhenry/git/conda-forge/.bot/fetch_ci_log.py "<targetUrl>" --lines 80

Classify root cause:
  - "missing-dep": solver error, "No candidates were found for <pkg>"
  - "build-bug": compilation/link error, wrong flags, wrong arch detection
  - "test-fail": tests run but produce wrong results
  - "transient": timeout, network error, disk full
  - "unknown": cannot determine
  - "none": all checks passing

Return JSON:
{
  "all_passing": false,
  "riscv64_passing": false,
  "failure_scope": "none",
  "root_cause": "none",
  "missing_dep_name": null,
  "log_excerpt": null,
  "failing_check_names": []
}
`, { label: 'ci-analysis', phase: 'Gather', schema: {
  type: 'object',
  required: ['all_passing', 'riscv64_passing', 'failure_scope', 'root_cause'],
  properties: {
    all_passing:         { type: 'boolean' },
    riscv64_passing:     { type: 'boolean' },
    failure_scope:       { type: 'string' },
    root_cause:          { type: 'string' },
    missing_dep_name:    {},
    log_excerpt:         {},
    failing_check_names: { type: 'array' },
  }
}})

// ── Phase 2: analyze ──────────────────────────────────────────────────────────

phase('Analyze')

const recommendation = await agent(`
You are deciding what action to take on a conda-forge riscv64 migration PR.
READ-ONLY mode — output a recommendation only, no action.

## Feedstock
Name: ${feedstock}  |  Repo: ${repo}  |  Descendants: ${args.num_descendants}

## PRs
Bot PR:  ${botPrUrl}
Best PR: ${bestPrUrl} (author: ${bestPrAuthor})
Same PR: ${!hasSeparateUserPr}
Migration tracker status: ${migrationStatus}

## PR state
${JSON.stringify(prState, null, 2)}

## Comments
${JSON.stringify(comments, null, 2)}

## CI analysis
${JSON.stringify(ciAnalysis, null, 2)}

## Prior state
  cat /home/luhenry/git/conda-forge/.bot/state/${feedstock}.json 2>/dev/null || echo "none"

---
Choose exactly ONE action:

NUDGE_MERGE       — riscv64 CI passing, PR not draft, no nudge in last 48h
WAIT_MISSING_DEP  — CI fails only due to missing riscv64 dep (solver error)
WAIT_UNRELATED_FAILURE — only non-riscv64 platforms fail; riscv64 is fine
NEEDS_FIX         — riscv64 CI fails due to a code/build bug
SKIP_ALREADY_HANDLED — last action < 6h ago and no status change
ESCALATE          — unclear situation, maintainer question, needs human judgment

Return JSON:
{
  "action": "",
  "confidence": "high",
  "reason": "",
  "details": "",
  "riscv64_ci_passing": false,
  "has_recent_nudge": false
}
`, { label: 'recommend', phase: 'Analyze', schema: {
  type: 'object',
  required: ['action', 'confidence', 'reason', 'details'],
  properties: {
    action:             { type: 'string' },
    confidence:         { type: 'string' },
    reason:             { type: 'string' },
    details:            { type: 'string' },
    riscv64_ci_passing: { type: 'boolean' },
    has_recent_nudge:   { type: 'boolean' },
  }
}})

return {
  feedstock,
  bot_pr_url:      botPrUrl,
  best_pr_url:     bestPrUrl,
  best_pr_author:  bestPrAuthor,
  num_descendants: args.num_descendants,
  pr_state:        prState,
  comments,
  ci_analysis:     ciAnalysis,
  recommendation,
}
