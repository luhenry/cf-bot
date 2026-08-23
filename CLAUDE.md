# conda-forge riscv64 migration bot

Tools and workflows for tracking and driving the linux-riscv64 migration on conda-forge.

## Architecture

Every deterministic behavior — dependency-graph traversal, CUDA/v1-bundle policy, the
authoritative "does this already have riscv64" check, state file I/O, shadow-dependency
reconciliation, migration-graph snapshots — lives in **`cf_core/`**, one Python package, and is
exposed as a CLI: `python3 -m cf_core <verb> ...` (run from this directory). The two workflow
files under `workflows/` call into it for anything deterministic and reserve actual LLM `agent()`
calls for genuinely fuzzy judgment: reading PR comment tone, classifying free-text CI failure
logs, drafting nudge text, and spotting a brand-new "depends on X" mention. `riscv64_status.py`
and `fetch_ci_log.py` are thin backward-compatible CLI shims over `cf_core` — same commands, same
output, so nothing that has them memorized needs to change.

```
cf_core/
  graph.py                    THE dependency-graph implementation (networkx). Single source of
                               truth — supersedes what used to be two independently-diverging
                               implementations (a hand-rolled BFS in riscv64_status.py and a
                               separate networkx script, graph_depth.py, now deleted).
  policy.py                   CUDA-wontfix pattern, v1-bundle detection, the action-code enums
                               (ACTION_CODES / TRACKED_ACTION_CODES — see "Actions" below),
                               PR-scoring weights, the commit-message attribution guard. Single
                               source of truth for every rule below — CLAUDE.md explains each
                               rule in prose, cf_core/policy.py is what actually enforces it.
  migration_source.py         fetches the migration JSON and arbitrary repo files at a ref
                               (raw.githubusercontent.com first, blobless partial-clone +
                               sparse-checkout fallback if that's blocked).
  conda_forge_yml_check.py    the authoritative riscv64-support check (see "Verification" below)
                               and the git-based PR-diffing helper.
  gh_client.py                `gh` CLI wrappers (PR list/view/comments, subscribe).
  ci_log.py                   CI log tail fetcher (GitHub Actions + Azure Pipelines).
  state_io.py                 validated, additive-merge, atomic read/write for state/*.json and
                               state/_tracked/*.json — see "State files" below.
  reconciler.py                re-verifies EXISTING state/_tracked/*.json entries (and
                               currently-"ready" feedstocks) against conda-forge.yml, throttled
                               by the existing `last_checked` field.
  snapshot.py                  persists a timestamped snapshot of the fetched migration graph
                               each run and diffs against the previous one.
  cli.py, __main__.py          `python3 -m cf_core ...` dispatcher — the seam the JS workflows
                               call through.
tests/                        `python3 -m unittest discover -s tests -v` (stdlib unittest, no
                               extra dependency to install). Includes a golden-fixture test
                               against a frozen real snapshot at tests/fixtures/migration_snapshot.json
                               and named regression tests for three real graph bugs found while
                               building this (see cf_core/graph.py's module docstring).
```

## Quick start

```bash
cd /home/luhenry/git/conda-forge/.bot
pip install -r requirements.txt   # networkx + pyyaml -- see requirements.txt for why these
                                   # are new hard dependencies as of the cf_core rearchitecture
```

### Check current migration status

```bash
python3 riscv64_status.py          # full analysis (queries GitHub for best PRs)
python3 riscv64_status.py --no-gh  # fast, uses migration JSON only
python3 riscv64_status.py --depth  # full analysis + depth-to-conda-forge-ci-setup priority

# equivalent, structured-JSON form used internally by the workflows:
python3 -m cf_core ready-list
python3 -m cf_core graph --target conda-forge-ci-setup
```

### Run a full triage

Analyzes every ready feedstock (in-PR, all deps done), reconciles existing tracked state against
ground truth, classifies each PR, writes state files, commits.

Tell Claude:
> Run the triage workflow

Claude will invoke `workflows/triage_migration.js` and summarize results.

### Check a single feedstock

Tell Claude:
> Analyze <feedstock-name>

Claude will run `workflows/analyze_feedstock.js` for that feedstock and report its recommendation.

### Run the test suite

```bash
python3 -m unittest discover -s tests -v
```

---

## Workflow architecture

```
triage_migration.js          — top-level, runs once per cron tick
  ├─ Fetch                     relay: cf_core ready-list (already-sorted, already-structured
  │                            JSON — no table-parsing) + cf_core graph --save-snapshot (also
  │                            answers "what changed since last run")
  ├─ Reconcile                 relay: cf_core reconcile — deterministically re-verifies EXISTING
  │                            state/_tracked/*.json entries and flags any "ready" feedstock
  │                            that's actually already done, BEFORE any LLM analysis runs
  ├─ Analyze                   per-feedstock, pipelined (pipeline(), not a sequential for-loop —
  │  └─ analyze_feedstock.js   each feedstock's chain runs independently, no cross-item barrier)
  │       ├─ Verify              relay: cf_core verify feedstock — CUDA-wontfix +
  │       │                      conda-forge.yml checks, zero LLM reasoning, short-circuits
  │       │                      WONTFIX_PLATFORM / SKIP_ALREADY_HANDLED before any gh call
  │       ├─ pr-state             fetch PR metadata + CI checks + changed files (fuzzy)
  │       ├─ comments             fetch PR comments, DISCOVER new shadow-dep mentions (fuzzy)
  │       ├─ ci-analysis          fetch failing CI logs, classify root cause (fuzzy)
  │       ├─ v1-check             relay: cf_core verify feedstock --pr-json — accurate
  │       │                      v1-bundle detection from the PR's real title/files
  │       └─ recommend            the one genuinely fuzzy judgment call: synthesize into one
  │                              action, given the deterministic checks as ground truth
  ├─ Shadow                    NEW shadow-dependency discovery only (fuzzy) — reconciling
  │                            EXISTING entries already happened in Reconcile, deterministically
  └─ Summarize                 relay: cf_core state write per result (no LLM "Write tool
                               directly" step) + relay: cf_core policy check-commit-message
                               (refuses to commit if it fails) + commit + run summary
```

All workflows are **read-only** (Phase 1+2 only). Phase 3 (act) is not automated — see
"Contributing to a feedstock" below.

## Cron

Triage runs automatically at 01:07, 09:07, 17:07 local time (every 8h).
Cron job id: `4b9a95d4`. Auto-expires after 7 days — recreate with:
> Schedule the triage cron every 8 hours

## State files

`state/<feedstock>.json` — written after every triage run, committed to git. **Field names,
types, and file location are stable** — this is the on-disk contract; `cf_core/state_io.py`
enforces it in code (additive-only merge, so writing a partial update never drops an existing
field; refuses to write an unrecognized `last_action`) rather than only in this prose.

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
  "num_descendants": 148,
  "depth_to_ci_setup": 2,
  "blocking_dependency_feedstocks": []
}
```

`state/_tracked/<name>.json` — **shadow dependencies**: feedstocks that gate the migration
but do not appear on the official migration page (see "Shadow dependencies" below).

`state/_snapshot/` — new, purely additive: `latest.json` plus one timestamped file per run,
used by `cf_core.snapshot` to answer "what changed since last run" (newly done, newly in-PR,
dropped-from-in-PR-without-a-matching-done — usually means merged). Never touches the two
directories above.

## Actions (Phase 1+2 — read-only)

Enforced in code: `cf_core/policy.py`'s `ACTION_CODES` (state/*.json) and
`TRACKED_ACTION_CODES` (state/_tracked/*.json, a superset — adds `DONE`/`WAIT_NOT_STARTED`/
`NEEDS_VERIFICATION`). `cf_core/state_io.py` refuses to write any value outside these sets.

| Action | Meaning |
|---|---|
| `NUDGE_MERGE` | riscv64 CI green, PR not draft, no recent nudge — ready to merge |
| `WAIT_MISSING_DEP` | CI fails because a dep isn't yet available for riscv64 |
| `WAIT_UNRELATED_FAILURE` | Only non-riscv64 platforms fail; riscv64 migration is fine |
| `NEEDS_FIX` | riscv64 CI fails due to a code/build bug |
| `NEEDS_MINIMAL_PR` | Only an unwanted PR exists (e.g. bundles a v1 migration) — a human needs to open a minimal bot-PR + fix instead. See "Never bundle v1 migration" below. |
| `WONTFIX_PLATFORM` | Structurally incompatible with riscv64 (e.g. requires CUDA) — not actionable, tracked for visibility only. See "CUDA / GPU-vendor platforms" below. |
| `WAIT_UPSTREAM` | Direction is already decided (not ambiguous), but the fix genuinely can't land yet because it depends on an upstream project shipping riscv64 support the feedstock itself can't produce (e.g. no upstream riscv64 release binary exists yet). Distinct from `WAIT_MISSING_DEP` (a conda-forge packaging gap, fixable within conda-forge) and `WONTFIX_PLATFORM` (permanently blocked). Re-check periodically — it's not a dead end, just not actionable today. |
| `ESCALATE` | Genuinely unclear situation or needs human judgment — NOT to be used just because an alternative v1-bundled PR exists (that's `NEEDS_MINIMAL_PR`), because the package is a known platform wontfix (that's `WONTFIX_PLATFORM`), or because the blocker is a decided-but-unshippable upstream gap (that's `WAIT_UPSTREAM`) |
| `SKIP_ALREADY_HANDLED` | Checked recently with no change — including when `cf_core reconcile`'s conda-forge.yml check finds a "ready" feedstock is actually already done (migration-page lag) |

`state/_tracked/*.json` only: `DONE` (confirmed via conda-forge.yml, auto-set by
`cf_core.reconciler`), `WAIT_NOT_STARTED` (no riscv64 PR open yet), `NEEDS_VERIFICATION`
(status genuinely unclear, needs a check).

## PR selection heuristics

Implemented once in `cf_core/policy.py` (`score_pr`), called from both `riscv64_status.py` and
the JS workflow via `cf_core verify feedstock --pr-json`. For each feedstock the bot PR is the
baseline; open PRs are scored to find a better alternative:

1. **Non-bot, riscv64-focused, no v1 migration** → score 100 (+20 if supersedes bot)
2. **Bot PR** → score 20 (preferred over v1-only alternatives)
3. **Non-bot, v1 migration + riscv64** → score 10 (+5 if supersedes bot)

The feedstock's current recipe format is checked first — if it already uses
`recipe.yaml`, adding one is not a v1 migration.

If the *only* open PR is tier 3 (v1-bundled) and there's no separate focused PR, the
scoring still surfaces it as `best_pr_url` — but the **recommend** step must not treat
that as mergeable or as grounds to escalate. See "Never bundle v1 migration" below;
the correct action there is `NEEDS_MINIMAL_PR`.

## Verification: authoritative riscv64-support check

Don't infer "does this feedstock already have riscv64?" from the migration JSON's
`done`/`in-pr` sets alone, or from a PR title/description — both can lag or mislead.
The authoritative check: read `conda-forge.yml` at the repo root on the `main` branch
and look for `linux_riscv64` under `build_platform`, e.g.:

```yaml
build_platform:
  linux_riscv64: linux_64
```

If it's not there, riscv64 support has **not** been merged yet, whatever the migration
page or a PR's status checks say. This is real code now, not just a documented manual
procedure: `cf_core.conda_forge_yml_check.check_feedstock(name)` (CLI: `python3 -m cf_core verify
feedstock <name>`, or `python3 -m cf_core policy check-cuda <name>` for the CUDA half alone). It's
wired into `cf_core.reconciler` so it runs automatically on every existing shadow-dependency
entry and every "ready" feedstock — not just when someone happens to ask.

Manual fallback (same technique the CLI uses under the hood, useful for a one-off check outside
this repo's tooling):

```bash
git init -q tmp-check && cd tmp-check
git remote add origin https://github.com/conda-forge/<pkg>-feedstock.git
git fetch -q --depth 1 origin main
git show origin/main:conda-forge.yml | grep -A2 build_platform
```

To inspect an exact PR's diff (`cf_core.conda_forge_yml_check.diff_pr(feedstock, pr_number)`,
CLI: `python3 -m cf_core verify diff-pr <name> <pr_number>`) — works without any `gh`/API access,
just plain git:

```bash
git fetch -q origin refs/pull/<pr-number>/head:pr-<pr-number>
git diff origin/main pr-<pr-number> -- conda-forge.yml recipe/
```

This is how the pandoc-feedstock#171 correctness bug was confirmed exactly (see
`state/pandoc.json`): the recipe's `# [linux and not aarch64]` source-URL selector for
the linux-amd64 binary also matches riscv64, so the PR would repackage an x86-64 binary
under the riscv64 label.

## Policy: never bundle v1 migration into the riscv64 path

There is a known issue with cross-compiled testing under the v1/rattler-build recipe
format. **riscv64 migration and v1 migration must never land in the same PR.** If they
do, do not recommend merging it, and do not just `ESCALATE` because "there are two
competing PRs" — that's not actually ambiguous, it's covered by this policy.

The correct move when only a v1-bundled PR exists (or when a maintainer has proposed one
alongside a focused fix): open a **new, minimal PR** = the bot's original migration diff
+ the smallest fix needed to get riscv64 CI green. Nothing else. Classify this situation
as `NEEDS_MINIMAL_PR`, not `ESCALATE`.

Detection (`cf_core.policy.pr_is_v1_migration`): a PR introduces v1 if it adds `recipe.yaml` or
its title mentions v1/rattler/pixi, *unless* the repo's `main` branch already uses `recipe.yaml`
(then nothing is being "migrated"). `analyze_feedstock.js` calls this with the PR's *real*
title/changed-files (fetched from `gh pr view`), not a guess — this closes a real gap from before
this rearchitecture, where the v1 flag computed by `riscv64_status.py` was never actually passed
through to the JS analysis step at all.

**Reference case — perl:** [perl-feedstock#76](https://github.com/conda-forge/perl-feedstock/pull/76)
is exactly this minimal PR (bot PR + the malloc/free type-def fix + absolute-path fix for
QEMU testing) and is the one to work from — not
[perl-feedstock#77](https://github.com/conda-forge/perl-feedstock/pull/77), wolfv's
separate draft that bundles the same riscv64 fixes with a v1/rattler-build conversion.
Once riscv64 CI is green on the minimal PR, recommend `NUDGE_MERGE` on it directly; the
v1-bundled alternative is out of scope and shouldn't block that recommendation.

This pattern shows up repeatedly, not just on perl — wolfv has opened the same
"Support linux-riscv64 and migrate recipe to v1" style PR on other foundational
feedstocks (e.g. expat-feedstock#73, readline-feedstock#38, mpdecimal-feedstock#11 as of
2026-08-21). Treat all of these the same way: `NEEDS_MINIMAL_PR`, not a PR to merge as-is.

## Policy: CUDA / GPU-vendor platforms are `WONTFIX_PLATFORM`

RISC-V has no NVIDIA CUDA toolkit — CUDA is not a supported host platform on riscv64,
full stop. Feedstocks that fundamentally require CUDA (name patterns: `cuda-*`, `libcu*`,
`nccl`, and similar NVIDIA-only packages) should be classified `WONTFIX_PLATFORM` and
excluded from active priority — they are not fixable work items, just permanently blocked
by platform. Known members as of 2026-08-21: `cuda-nvml-dev`, `libcurand`, `nccl`.

Detection (`cf_core.policy.is_cuda_wontfix` / `CUDA_WONTFIX_RE`): one implementation, called from
`riscv64_status.py` and, via `cf_core verify feedstock`, from `analyze_feedstock.js` — this is
the single source of truth; there is no separate JS-side copy of this pattern to keep in sync
(there used to be, before this rearchitecture, and it had already started drifting).

Note: this is about packages that themselves *require* CUDA to build/run, not packages
that merely optionally depend on it — Ludovic already removed the CUDA dependency from
`mpich` and `openmpi` for riscv64, so those two are **not** wontfix and should stay in
normal tracking. If a CUDA-pattern name shows up that turns out to be CUDA-optional
(builds fine riscv64-only via a variant without CUDA), don't wontfix it — check first.

## Priority: depth to `conda-forge-ci-setup`

Priority among ready feedstocks is **not** just `num_descendants`. The primary signal is
**depth of the dependency chain to `conda-forge-ci-setup`** — landing that package is the
#1 target, since it's the CI tooling infrastructure that (transitively) gates proper
linting/rerendering/testing for every other feedstock on riscv64. A feedstock that sits a
short hop from unblocking `conda-forge-ci-setup` should be prioritized over one with a
bigger raw descendant count but no direct relationship to it.

Computed by `cf_core.graph` — the single graph implementation (networkx-based; see the module's
docstring for the edge-direction convention, which is load-bearing and easy to get backwards).
`depth_to_target(G, "conda-forge-ci-setup")` does the BFS (shortest hop-count from
`conda-forge-ci-setup` to each of its transitive dependencies); `cli.py`'s `ready-list` and
`graph` verbs, and `riscv64_status.py --depth`, all call through it. Sort order:
`(depth_to_ci_setup ascending, num_descendants descending)`.

**Fetching the migration JSON**: `cf_core.migration_source.fetch_migration_json()` tries a direct
HTTPS GET first (`raw.githubusercontent.com`), then falls back to a blobless partial `git clone` +
non-cone `git sparse-checkout` of just `status/migration_json/supportlinuxriscv64platform.json`
from `github.com/conda-forge/conda-forge-bot-data` (~10s) if the direct GET fails. This
matters because some sandboxed environments (e.g. a restricted Cowork cloud session) block
`raw.githubusercontent.com` and the GitHub REST API (`api.github.com/repos/...` 403s with
"GitHub access to this repository is not enabled for this session") while still allowing
plain `git clone` over `https://github.com/...` — a different code path (smart-HTTP git
protocol vs. REST API). **This only gets you the file contents of a public repo** — it does
NOT unblock PR/issue/CI-check API calls (`gh pr view`, etc.), which still need real `gh`
access (see `cf_core/gh_client.py`). The same module also exposes `fetch_file_at_ref` (an
arbitrary file at an arbitrary ref, generalized for `conda_forge_yml_check.py`) and
`diff_pr_files` (a PR's head ref diffed against `main`, no `gh` needed at all).

**Real numbers as of 2026-08-21** (314 total feedstocks tracked in the migration graph that day;
re-run `python3 -m cf_core graph` for current numbers — the migration moves, don't treat these as
static):
`conda-forge-ci-setup` transitively depends on **242 of the 314** (77%) — it has 46 direct
(depth-1) dependencies including `python`, `conda`, `mamba`, `git`, `zstd`, and a long tail
of Python tooling (`mypy`, `astroid`, `debugpy`, `pydantic-core`, `tornado`, etc. — likely
conda-smithy's own lint/tooling dependency closure). The **longest simple chain** from
`conda-forge-ci-setup` down through its prerequisites was **24 hops** (through R-language
tooling → GSL → BLAS/LAPACK → MPICH → CUDA/RDMA/systemd packages → a large toolchain
bootstrap cluster → fontconfig/freetype/libpng/zlib) — not itself a priority signal (it's
an edge case through largely-unrelated packages), but it shows the graph has real depth and
three genuine cycles (non-trivial SCCs) baked into it, all plausible cross-compiler
bootstrap situations, not data errors:
- 12 members: `cctools-and-ld64`, `compiler-rt`, `libxcb`, `llvmdev`, `ncurses`, `openmp`,
  `python`, `readline`, `sqlite`, `xorg-libx11`, `xorg-libxft`, `xorg-libxrender`
- 2 members: `blas` ↔ `lapack`
- 2 members: `conda` ↔ `conda-pypi`

`cf_core.graph.depth_to_target`'s BFS handles this fine (BFS doesn't care about cycles for
shortest-path purposes); `find_cycles` and `longest_hop_chain` handle them explicitly via
strongly-connected-component condensation (a naive recursive DFS crashes/loops on a real cycle —
that's exactly the bug an earlier draft of this logic shipped with, see `cf_core/graph.py`'s
module docstring and `tests/test_graph.py`'s named regression tests).

`conda-forge-ci-setup-feedstock` itself had **no open riscv64 PR** as of 2026-08-21 — the migration
hadn't started there. `python` is a **direct (depth-1)** dependency of `conda-forge-ci-setup`
— among the highest-priority items, alongside `zstd` (also depth-1, and **confirmed done** via
the conda-forge.yml check — `state/_tracked/zstd.json` should read `DONE`, auto-reconciled going
forward). `python-feedstock#896` is itself blocked on `expat`, `libffi`, `mpdecimal`, `readline`,
`sqlite`, `xorg-libx11` (all depth-2) — **not** `tk`, which turned out not to be an actual graph
dependency of `python` despite being mentioned in PR comments (locked in as a regression test in
`tests/test_graph.py`). Of the depth-2 blockers: `libffi` was ready-to-merge; `expat`/`readline`/
`mpdecimal` only had v1-bundled PRs open (`NEEDS_MINIMAL_PR`, see policy above); `sqlite`/
`xorg-libx11` had no riscv64 PR open at all. These are tracked via the shadow dependency
mechanism below.

## Shadow dependencies (off the migration page)

Some blocking packages never show up in our "ready" list because they're not yet
`in-pr` on the official migration page (or the page doesn't cover them at all) — they
only surface indirectly, mentioned in a CI failure ("No candidates were found for X") or
a PR comment ("depends on X-feedstock", "blocked on X", "waiting on X-feedstock PR").
`python` is the running example: it's a hard blocker for a large fraction of the
`WAIT_MISSING_DEP` queue (`cross-python_linux-riscv64` failures) but isn't itself on the
migration page.

This has two halves, handled differently:

- **Discovery** (finding a name never tracked before) stays a fuzzy free-text task —
  `analyze_feedstock.js`'s `ci-analysis` and `comments` agents capture a
  `blocking_dependency_feedstocks: string[]` field, `triage_migration.js`'s `Shadow` phase dedupes
  against the official ready list and against already-tracked names, and writes the initial
  `state/_tracked/<name>.json` record via `cf_core state write --tracked` (never an LLM "Write
  tool directly" step).
- **Reconciliation** (re-checking an entry that's already tracked) is now fully deterministic —
  `cf_core.reconciler.reconcile_tracked()`, run in the `Reconcile` phase at the *start* of every
  triage run, re-verifies every non-terminal entry against `conda-forge.yml` (throttled by the
  existing `last_checked` field, terminal states `DONE`/`WONTFIX_PLATFORM` skipped outright) and
  flips stale actions on its own. Before this existed, an entry could only change when *that
  run's* LLM agents happened to re-mention it — `zstd` sat at `NEEDS_VERIFICATION` long after it
  had actually shipped riscv64, because nothing ever re-checked it. That's what this phase fixes.

## Helpers

`fetch_ci_log.py <url> [--lines N]` — fetch tail of a CI job log (backward-compat shim over
`cf_core.ci_log`). Supports GitHub Actions and Azure Pipelines URLs.

```bash
python3 fetch_ci_log.py "https://github.com/.../actions/runs/.../job/..." --lines 40
python3 fetch_ci_log.py "https://dev.azure.com/conda-forge/...?buildId=..." --lines 40
```

---

## Contributing to a feedstock (manual, Phase 3)

Phase 3 (act) is not automated by the workflows above. When Ludovic decides to actually
push a fix or nudge a PR forward, follow this procedure exactly — every step is required,
not optional shortcuts.

**All of this is run from `conda-forge/` at the repo root** (the parent of this `.bot`/`cf-bot`
checkout), not from inside `.bot`.

1. **Fork the feedstock**, using the `gh` CLI — this exact command, no variations:

   ```bash
   gh repo fork --clone --fork-name conda-forge-<pkg>-feedstock \
     https://github.com/conda-forge/<pkg>-feedstock <pkg>-feedstock
   ```

   This creates the fork under `github.com/luhenry` and clones it locally into
   `conda-forge/<pkg>-feedstock`.

2. **If a PR already exists** (from the bot or another contributor) and the work needs to
   build on it: after step 1 has created and cloned the fork, `cd` into
   `conda-forge/<pkg>-feedstock` and check out the PR with:

   ```bash
   gh pr checkout <pr-number>
   ```

   Use this exact command — do not manually add remotes or fetch refs by hand.

3. **Subscribe to the PR's notifications** (equivalent of clicking "Subscribe" on the web
   UI), via `gh api` — there's no dedicated `gh` subcommand for this:

   ```bash
   gh api -X PUT "repos/conda-forge/<pkg>-feedstock/issues/<pr-number>/subscription" \
     -f subscribed=true -f ignored=false
   ```

   Do this for every PR you fork/checkout/open, so Ludovic gets notified of new activity
   on it. (Not yet verified end-to-end in this environment — confirm the endpoint responds
   `200` the first time you run it, and flag here if it doesn't.)

4. **Activate the `conda-smithy` conda environment** before doing any recipe work:

   ```bash
   conda activate conda-smithy
   ```

5. **Before pushing**, always:
   - Run `conda-smithy lint` and fix any issues it reports.
   - Run `conda-smithy rerender -c auto`.
   - Commit manual recipe changes and the `conda-smithy rerender -c auto` output as
     **separate commits** — never squash them together — so it stays obvious which
     changes were hand-written and which were auto-generated.

### Attribution rule — non-negotiable

**Never** add any mention of Claude to a commit or PR: no `Co-Authored-By: Claude`, no
`Generated with Claude`, no similar tagline, in any commit message or PR description,
under any circumstances. Ludovic is the sole author of record for all of these changes.
This rule has no exceptions.

This is now also a deterministic safety net, not just a rule stated here: `triage_migration.js`'s
`Summarize` phase runs `cf_core policy check-commit-message` on the composed commit message
before every `git commit` and refuses to commit if it fails (see `cf_core/policy.py`'s
`BANNED_COMMIT_TERMS`). That guard only covers the automated triage commit — it does **not**
apply to Phase-3 manual work above, where the same rule still applies by hand, with the same
zero exceptions.
