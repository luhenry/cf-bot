export const meta = {
  name: 'analyze-feedstock',
  description: 'Phase 1+2: gather PR state, comments, CI results and decide action (read-only)',
  phases: [
    { title: 'Gather', detail: 'fetch PR state, comments, and CI checks in parallel' },
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

// ── Phase 1: gather ───────────────────────────────────────────────────────────

phase('Gather')

const [prState, comments, ciAnalysis] = await parallel([

  // Agent A – PR metadata and checks
  () => agent(`
You are gathering facts about a conda-forge feedstock PR. Do not interpret — only report.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl}

Run these commands and return the results as a JSON object:

1. gh pr view ${bestPrNumber} --repo ${repo} \\
     --json number,title,author,state,isDraft,createdAt,updatedAt,labels,baseRefName

2. gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup \\
   then extract the list of checks, keeping: name, status, conclusion, targetUrl

3. If bestPrUrl != botPrUrl (bot PR is separate), also fetch:
   gh pr view ${botPrNumber} --repo ${repo} --json number,title,state,statusCheckRollup

Return JSON:
{
  "pr": { number, title, author, isDraft, createdAt, updatedAt, labels: [] },
  "checks": [ { name, status, conclusion, url } ],
  "bot_pr_open": true|false,   // only if there is a separate bot PR
  "bot_pr_checks": [ ... ]     // only if there is a separate bot PR
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
  }}),

  // Agent B – PR comments
  () => agent(`
You are reading comments on a conda-forge feedstock PR. Do not interpret — only report.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl} (number: ${bestPrNumber})

Run:
  gh api repos/${repo}/issues/${bestPrNumber}/comments \\
    --jq '.[] | {author: .user.login, date: .created_at, body: .body}'

For each comment summarize:
- author
- date (ISO)
- a one-sentence summary of what it says
- flags:
    is_linter: true if from conda-forge-admin and about linting
    is_rerun_request: true if it contains "@conda-forge-admin, please rerun bot"
    is_our_bot: true if author is "luhenry-conda-forge-bot" (our automation account)
    is_failure_explanation: true if it explains a CI failure or missing dependency
    is_maintainer_question: true if the PR author or a maintainer is asking a question

${hasSeparateUserPr ? `Also fetch comments on the bot PR (number ${botPrNumber}):
  gh api repos/${repo}/issues/${botPrNumber}/comments \\
    --jq '.[] | {author: .user.login, date: .created_at, body: .body}'
` : ''}

Return JSON:
{
  "best_pr_comments": [ { author, date, summary, is_linter, is_rerun_request, is_our_bot, is_failure_explanation, is_maintainer_question } ],
  "bot_pr_comments":  [ ... ]   // empty array if no separate bot PR
}
`, { label: 'comments', phase: 'Gather', schema: {
    type: 'object',
    required: ['best_pr_comments', 'bot_pr_comments'],
    properties: {
      best_pr_comments: { type: 'array' },
      bot_pr_comments:  { type: 'array' },
    }
  }}),

  // Agent C – CI failure analysis (runs regardless; returns quickly if all passing)
  () => agent(`
You are diagnosing CI failures on a conda-forge feedstock PR. Do not suggest fixes — only classify.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl}
Migration tracker status: ${migrationStatus}

Step 1 — identify failing checks.
Run:
  gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup

Look for checks where conclusion == "FAILURE" or status == "FAILURE".

Step 2 — for each failing check, classify it:
  - "riscv64-only": only linux_riscv64_* checks fail, everything else passes
  - "unrelated": the failure is on a non-riscv64 platform (e.g. win_64_, osx_*)
  - "all-platforms": failures span multiple platforms including riscv64
  - "pending": check hasn't completed yet

Step 3 — for up to 2 failing linux_riscv64_* checks, fetch the last 80 lines of
the build log to identify the root cause.

To get a job log:
  # First find the run ID from the check's targetUrl (it contains /runs/<id>/ or buildId=)
  # For GitHub Actions URLs (github.com/...actions/runs/<runId>/job/<jobId>):
  gh api repos/${repo}/actions/jobs/<jobId>/logs 2>&1 | tail -80
  # For Azure Pipelines URLs: just note the URL, do not try to fetch the log

Classify the root cause as one of:
  - "missing-dep": solver error, "No candidates were found for <pkg>", package not available for riscv64
  - "build-bug": compilation error, link error, wrong flags, wrong arch detection
  - "test-fail": tests run but produce wrong results
  - "transient": timeout, network error, disk full, runner error
  - "unknown": cannot determine from available log

Return JSON:
{
  "all_passing": true|false,
  "riscv64_passing": true|false,
  "failure_scope": "riscv64-only"|"unrelated"|"all-platforms"|"pending"|"none",
  "root_cause": "missing-dep"|"build-bug"|"test-fail"|"transient"|"unknown"|"none",
  "missing_dep_name": "pkg-name or null",
  "log_excerpt": "last relevant error lines, max 20 lines, or null",
  "failing_check_names": []
}
`, { label: 'ci-analysis', phase: 'Gather', schema: {
    type: 'object',
    required: ['all_passing', 'riscv64_passing', 'failure_scope', 'root_cause'],
    properties: {
      all_passing:        { type: 'boolean' },
      riscv64_passing:    { type: 'boolean' },
      failure_scope:      { type: 'string' },
      root_cause:         { type: 'string' },
      missing_dep_name:   {},
      log_excerpt:        {},
      failing_check_names:{ type: 'array' },
    }
  }}),
])

// ── Phase 2: analyze ──────────────────────────────────────────────────────────

phase('Analyze')

const stateFile = `.bot/state/${feedstock}.json`

const recommendation = await agent(`
You are deciding what action (if any) to take on a conda-forge riscv64 migration PR.
You are in READ-ONLY mode. You will output a recommendation, not take any action.

## Feedstock
Name: ${feedstock}
Repo: ${repo}
Descendants blocked by this feedstock: ${args.num_descendants}

## PRs
Bot PR:  ${botPrUrl}
Best PR: ${bestPrUrl} (author: ${bestPrAuthor})
Are they the same PR? ${!hasSeparateUserPr}
Migration tracker status: ${migrationStatus}

## PR state
${JSON.stringify(prState, null, 2)}

## Comments
${JSON.stringify(comments, null, 2)}

## CI analysis
${JSON.stringify(ciAnalysis, null, 2)}

## State file (${stateFile})
Read the state file if it exists:
  cat ${stateFile} 2>/dev/null || echo "no prior state"

---

Based on all the above, recommend exactly ONE action from this list:

NUDGE_MERGE
  When: riscv64 CI is passing, PR is not draft, no recent nudge in the last 48h.
  The PR is ready to merge but the maintainer hasn't acted.

WAIT_MISSING_DEP
  When: CI fails only because a dependency is not yet available for riscv64.
  The build error is a solver/dependency failure, not a code bug.
  Nothing to do until the upstream dep lands.

WAIT_UNRELATED_FAILURE
  When: the only CI failures are on non-riscv64 platforms (e.g. win_64_ fails
  but linux_riscv64_ passes). The riscv64 migration itself is fine.

NEEDS_FIX
  When: riscv64 CI fails due to a build bug, wrong flags, test failure, or
  anything that is a code problem rather than a missing dependency.
  The PR needs a code change to proceed.

SKIP_ALREADY_HANDLED
  When: the last action in the state file was taken less than 6 hours ago and
  the PR status hasn't changed since then.

ESCALATE
  When: situation is unclear, contradictory, or needs human judgment.
  Examples: maintainer asked a question, conflicting PRs, CI is flapping.

Return JSON:
{
  "action": "<one of the above>",
  "confidence": "high"|"medium"|"low",
  "reason": "one sentence explaining why",
  "details": "2-3 sentences with supporting evidence from the data above",
  "riscv64_ci_passing": true|false,
  "has_recent_nudge": true|false
}
`, { label: 'recommend', phase: 'Analyze', schema: {
  type: 'object',
  required: ['action', 'confidence', 'reason', 'details'],
  properties: {
    action:            { type: 'string' },
    confidence:        { type: 'string' },
    reason:            { type: 'string' },
    details:           { type: 'string' },
    riscv64_ci_passing:{ type: 'boolean' },
    has_recent_nudge:  { type: 'boolean' },
  }
}})

// ── Return result ──────────────────────────────────────────────────────────────

return {
  feedstock,
  bot_pr_url:   botPrUrl,
  best_pr_url:  bestPrUrl,
  best_pr_author: bestPrAuthor,
  num_descendants: args.num_descendants,
  pr_state:     prState,
  comments:     comments,
  ci_analysis:  ciAnalysis,
  recommendation,
}
