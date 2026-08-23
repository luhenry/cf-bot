export const meta = {
  name: 'analyze-feedstock',
  description: 'Deterministic pre-checks via cf_core (incl. fork/clone + riscv64 PR subscribe), then gather PR state/comments/CI and decide action',
  phases: [
    { title: 'Verify', detail: 'CUDA / conda-forge.yml checks, fork+clone, riscv64 PR notification subscribe -- all deterministic, no LLM reasoning' },
    { title: 'Gather', detail: 'fetch PR state, comments, CI checks -- genuinely fuzzy free-text work' },
    { title: 'Analyze', detail: 'synthesize findings into a recommended action' },
  ],
}

// args: { name, bot_pr_url, best_pr_url, best_pr_author, pr_status, num_descendants, depth_to_ci_setup }

const feedstock = args.name
const repo = `conda-forge/${feedstock}-feedstock`
const botPrUrl = args.bot_pr_url
const bestPrUrl = args.best_pr_url
const bestPrAuthor = args.best_pr_author
const migrationStatus = args.pr_status
const bestPrNumber = bestPrUrl.match(/\/pull\/(\d+)/)?.[1]
const botPrNumber  = botPrUrl.match(/\/pull\/(\d+)/)?.[1]
const hasSeparateUserPr = bestPrUrl !== botPrUrl

const CF_BOT_DIR = '/home/luhenry/git/conda-forge/.bot'

function skipResult(action, reason, details, riscv64CiPassing) {
  return {
    feedstock,
    bot_pr_url: botPrUrl,
    best_pr_url: bestPrUrl,
    best_pr_author: bestPrAuthor,
    num_descendants: args.num_descendants,
    // `verify` (the Phase 0 deterministic pre-check, computed before either short-circuit that
    // calls this helper) already forked/cloned the feedstock and subscribed to its open riscv64
    // PRs as a side effect -- surface that here too, not just on the non-short-circuited path.
    riscv64_pr_subscriptions: verify.conda_forge_yml?.riscv64_pr_subscriptions ?? [],
    pr_state: null,
    comments: null,
    ci_analysis: null,
    blocking_dependency_feedstocks: [],
    recommendation: {
      action,
      confidence: 'high',
      reason,
      details,
      riscv64_ci_passing: riscv64CiPassing,
      has_recent_nudge: false,
    },
  }
}

// ── Phase 0: deterministic pre-checks (cf_core, zero LLM reasoning) ──────────────────────────
//
// Before this rearchitecture, only the CUDA check was short-circuited this way, and even then
// via a JS regex maintained as a SEPARATE constant from the Python one in riscv64_status.py --
// two independent copies of one rule, free to diverge. Every deterministic predicate now has
// exactly one implementation (cf_core.policy / cf_core.conda_forge_yml_check), called here via
// a minimal-effort relay agent whose only job is to run the exact command and hand back its
// JSON verbatim -- no reasoning, no restated policy prose, no drift risk.
//
// This single call also forks+clones the feedstock (if not already local) and subscribes to
// every open riscv64-related PR on it (cf_core.gh_client.subscribe_to_riscv64_prs) -- see
// cf_core/conda_forge_yml_check.py's module docstring. There used to be a separate Setup phase
// for this, using the JS layer's own bestPrNumber/botPrNumber; it's gone now because this call
// already does the fork/clone as a side effect of the conda-forge.yml check, and the riscv64 PR
// search is a superset of "just the two PR numbers we already knew about."

phase('Verify')

const verify = await agent(`
Run exactly this command and return its stdout JSON verbatim as your answer. Do not interpret,
summarize, or modify it in any way -- just run it and report the JSON it printed.

  cd ${CF_BOT_DIR} && python3 -m cf_core verify feedstock ${feedstock}
`, { label: 'verify', phase: 'Verify', effort: 'low', schema: {
  type: 'object',
  required: ['feedstock', 'is_cuda_wontfix', 'conda_forge_yml'],
  properties: {
    feedstock: { type: 'string' },
    is_cuda_wontfix: { type: 'boolean' },
    conda_forge_yml: {
      type: 'object',
      properties: {
        checked: { type: 'boolean' },
        has_riscv64: {},
        error: {},
        source: {},
        riscv64_pr_subscriptions: { type: 'array' },
      },
    },
  },
}})

if (verify.is_cuda_wontfix) {
  return skipResult(
    'WONTFIX_PLATFORM',
    'CUDA is not a supported host platform on RISC-V (no NVIDIA CUDA toolkit for riscv64). Structural platform incompatibility, not a fixable build issue.',
    'Determined by cf_core.policy.is_cuda_wontfix before any GitHub calls.',
    false,
  )
}

if (verify.conda_forge_yml?.checked && verify.conda_forge_yml.has_riscv64) {
  // The authoritative check (conda-forge.yml on main) says this is already merged. The
  // migration page's done/in-pr sets can lag behind an actual merge -- don't burn an LLM
  // analysis cycle re-litigating something that's already finished.
  return skipResult(
    'SKIP_ALREADY_HANDLED',
    'conda-forge.yml on main already has linux_riscv64 under build_platform -- already merged; migration-page status is lagging behind reality.',
    'Determined by cf_core.conda_forge_yml_check before any GitHub calls.',
    true,
  )
}

// ── Phase 1: gather (sequential) -- the genuinely fuzzy free-text work stays here ────────────

phase('Gather')

const prState = await agent(`
You are gathering facts about a conda-forge feedstock PR. Do not interpret -- only report.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl}

Run these commands and return the results as a JSON object:

1. gh pr view ${bestPrNumber} --repo ${repo} \\
     --json number,title,author,state,isDraft,createdAt,updatedAt,labels,baseRefName,files \\
   -- keep the file list as a plain array of path strings (files[].path).

2. gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup \\
   then extract the list of checks, keeping: name, status, conclusion, targetUrl

3. If bot PR is separate (${hasSeparateUserPr}), also fetch:
   gh pr view ${botPrNumber} --repo ${repo} --json number,title,state

Return JSON:
{
  "pr": { "number": 0, "title": "", "author": "", "isDraft": false, "createdAt": "", "updatedAt": "", "labels": [], "files": [] },
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
You are reading comments on a conda-forge feedstock PR. Do not interpret -- only report.

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

Also scan every comment body for mentions of OTHER feedstocks blocking this one -- phrases
like "depends on X", "it has a dependency on X", "blocked on X-feedstock", "waiting on
X-feedstock PR #N", "X-feedstock must land first". Extract the bare feedstock name (strip
any "-feedstock" suffix) into blocking_dependency_feedstocks. Only include names that are
NOT "${feedstock}" itself. If none, return an empty array. This shadow-dependency DISCOVERY
step stays a fuzzy free-text task even after this rearchitecture -- only the ongoing
RECONCILIATION of already-discovered entries became deterministic (see cf_core.reconciler).

${hasSeparateUserPr ? `Also fetch bot PR comments (number ${botPrNumber}):
  gh api repos/${repo}/issues/${botPrNumber}/comments \\
    --jq '.[] | {author: .user.login, date: .created_at, body: .body}'
` : ''}

Return JSON:
{
  "best_pr_comments": [ { "author": "", "date": "", "summary": "", "is_linter": false, "is_rerun_request": false, "is_our_bot": false, "is_failure_explanation": false, "is_maintainer_question": false } ],
  "bot_pr_comments": [],
  "blocking_dependency_feedstocks": []
}
`, { label: 'comments', phase: 'Gather', schema: {
  type: 'object',
  required: ['best_pr_comments', 'bot_pr_comments'],
  properties: {
    best_pr_comments: { type: 'array' },
    bot_pr_comments:  { type: 'array' },
    blocking_dependency_feedstocks: { type: 'array' },
  }
}})

const ciAnalysis = await agent(`
You are diagnosing CI failures on a conda-forge feedstock PR. Do not suggest fixes -- only classify.

Feedstock: ${feedstock}
Repo: ${repo}
PR: ${bestPrUrl} (number: ${bestPrNumber})
Migration tracker status: ${migrationStatus}

Step 1 -- identify failing checks:
  gh pr view ${bestPrNumber} --repo ${repo} --json statusCheckRollup

Look for checks where conclusion == "FAILURE" or status == "FAILURE".

Step 2 -- classify failure scope:
  - "riscv64-only": only linux_riscv64_* checks fail
  - "unrelated": failure only on non-riscv64 platforms (win_64_, osx_*, etc.)
  - "all-platforms": failures span riscv64 and other platforms
  - "pending": checks still running
  - "none": all passing

Step 3 -- for up to 2 failing checks, fetch the log (prioritize linux_riscv64_* first):
  python3 ${CF_BOT_DIR}/fetch_ci_log.py "<targetUrl>" --lines 80

Classify root cause:
  - "missing-dep": solver error, "No candidates were found for <pkg>"
  - "build-bug": compilation/link error, wrong flags, wrong arch detection
  - "test-fail": tests run but produce wrong results
  - "transient": timeout, network error, disk full
  - "unknown": cannot determine
  - "none": all checks passing

If root_cause is "missing-dep", extract the bare feedstock name of the missing package
(strip version pins / build strings, e.g. "cross-python_linux-riscv64" -> "cross-python",
"libtiff 4.7.*" -> "libtiff") into missing_dep_name AND also add it to
blocking_dependency_feedstocks.

Return JSON:
{
  "all_passing": false,
  "riscv64_passing": false,
  "failure_scope": "none",
  "root_cause": "none",
  "missing_dep_name": null,
  "log_excerpt": null,
  "failing_check_names": [],
  "blocking_dependency_feedstocks": []
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
    blocking_dependency_feedstocks: { type: 'array' },
  }
}})

const blockingDependencyFeedstocks = Array.from(new Set([
  ...(comments.blocking_dependency_feedstocks ?? []),
  ...(ciAnalysis.blocking_dependency_feedstocks ?? []),
].filter(Boolean)))

// Second deterministic pre-check: now that we know the PR's real title/changed-files (from
// prState, fetched above), get an accurate v1-bundle classification -- this replaces the old
// design's full-English policy restatement inside the `recommend` prompt, which existed only
// because the v1-bundle flag was never actually threaded through from riscv64_status.py's own
// (separate, Python-only) v1 detection in the first place.

const prMeta = prState?.pr ?? {}
const prFiles = (prMeta.files ?? []).map(f => (typeof f === 'string' ? f : f.path)).filter(Boolean)

const v1Check = await agent(`
Run exactly this command and return its stdout JSON verbatim as your answer:

  cd ${CF_BOT_DIR} && python3 -m cf_core verify feedstock ${feedstock} --pr-json '${JSON.stringify({
    title: prMeta.title ?? '',
    files: prFiles,
    author_login: prMeta.author ?? '',
    bot_pr_number: Number(botPrNumber) || 0,
  }).replace(/'/g, "'\\''")}'
`, { label: 'v1-check', phase: 'Gather', effort: 'low', schema: {
  type: 'object',
  properties: {
    repo_is_v1: { type: 'boolean' },
    pr_is_v1_migration: { type: 'boolean' },
    pr_score: { type: 'number' },
  },
}})

// ── Phase 2: analyze -- the one genuinely fuzzy judgment call left ───────────────────────────

phase('Analyze')

const recommendation = await agent(`
You are deciding what action to take on a conda-forge riscv64 migration PR.
READ-ONLY mode -- output a recommendation only, no action.

## Feedstock
Name: ${feedstock}  |  Repo: ${repo}  |  Descendants: ${args.num_descendants}  |  Depth to conda-forge-ci-setup: ${args.depth_to_ci_setup ?? '?'}

## PRs
Bot PR:  ${botPrUrl}
Best PR: ${bestPrUrl} (author: ${bestPrAuthor})
Same PR: ${!hasSeparateUserPr}
Migration tracker status: ${migrationStatus}

## Deterministic pre-checks (already run by cf_core -- these are ground truth, act on them,
## do not re-derive or second-guess them)
- pr_is_v1_migration: ${v1Check.pr_is_v1_migration ?? 'unknown'} (repo already uses v1 recipe format: ${v1Check.repo_is_v1 ?? 'unknown'})
- is_cuda_wontfix: false (this call would already have returned WONTFIX_PLATFORM otherwise)
- conda-forge.yml already has riscv64: false (this call would already have returned SKIP_ALREADY_HANDLED otherwise)

## PR state
${JSON.stringify(prState, null, 2)}

## Comments
${JSON.stringify(comments, null, 2)}

## CI analysis
${JSON.stringify(ciAnalysis, null, 2)}

## Blocking dependency feedstocks mentioned (CI logs + comments)
${JSON.stringify(blockingDependencyFeedstocks)}

## Prior state
  python3 -m cf_core state read ${feedstock}

---
## Policy (enforced in code by cf_core.policy -- summarized here as a reminder, not the source of truth)

- If pr_is_v1_migration is true: do NOT recommend merging this PR as-is, and do NOT escalate
  merely because "there's ambiguity" -- there isn't. If a separate focused (non-v1) PR is also
  open and riscv64-green, that's not this case (best_pr_url would already point at it). If the
  ONLY option is this v1-bundled PR, recommend NEEDS_MINIMAL_PR -- a human needs to open a new
  PR = bot's migration diff + the smallest fix needed, no v1 changes. Reference case:
  perl-feedstock#76 (the minimal PR) vs #77 (wolfv's v1-bundled draft) -- #76 is what should have
  been picked.
- WAIT_UPSTREAM: use when the blocker is a decided-but-unshippable upstream gap -- the fix can't
  land because it depends on an upstream project shipping riscv64 support the feedstock itself
  can't produce (e.g. no upstream riscv64 release binary exists yet, confirmed by checking recent
  upstream releases). Distinct from WAIT_MISSING_DEP (a conda-forge packaging gap, fixable within
  conda-forge).
- ESCALATE is for genuinely unclear cases only -- a real open question needing human judgment (a
  correctness concern, a maintainer objection, conflicting non-v1 PRs). Do not use it as a
  catch-all for anything a deterministic pre-check or the policy above already resolves.

Choose exactly ONE action:

NUDGE_MERGE            -- riscv64 CI passing, PR not draft, no nudge in last 48h
WAIT_MISSING_DEP       -- CI fails only due to missing riscv64 dep (solver error)
WAIT_UNRELATED_FAILURE -- only non-riscv64 platforms fail; riscv64 is fine
NEEDS_FIX              -- riscv64 CI fails due to a code/build bug
NEEDS_MINIMAL_PR       -- only a v1-bundled (or otherwise unwanted) PR exists
WAIT_UPSTREAM          -- decided direction, blocked on an upstream release (see policy above)
SKIP_ALREADY_HANDLED   -- last action < 6h ago and no status change
ESCALATE               -- genuinely unclear, needs human judgment (see policy above)

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
  riscv64_pr_subscriptions: verify.conda_forge_yml?.riscv64_pr_subscriptions ?? [],
  pr_state:        prState,
  comments,
  ci_analysis:     ciAnalysis,
  blocking_dependency_feedstocks: blockingDependencyFeedstocks,
  recommendation,
}
