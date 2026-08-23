"""
cf_core.reconciler — periodic reconciliation of existing state against ground truth.

Before this module existed, `state/_tracked/<name>.json` entries were only updated reactively --
when THAT run's LLM agents happened to mention the name in a PR comment or CI log. Existing
entries were never re-checked on their own initiative: `state/_tracked/zstd.json` sat at
`NEEDS_VERIFICATION` long after zstd had actually shipped riscv64 support, because nothing in the
pipeline ever re-verified it -- a human had to notice and fix it by hand this session. This
module closes that gap: it walks every existing state/_tracked/*.json entry (and, for the ready
migration-page list, offers a matching check) and re-verifies each via
cf_core.conda_forge_yml_check, flipping stale actions deterministically.

Throttled by the *existing* `last_checked` field (not a new one) so this isn't re-checking every
tracked feedstock's GitHub state on every single run -- see DEFAULT_THROTTLE.

Note: `cf_core.conda_forge_yml_check.check_feedstock` forks+clones a feedstock via `gh` the first
time it's asked about it, and subscribes to its open riscv64 PRs on *every* call (see that
module's docstring) -- so a shadow dependency reconciled here, or a "ready" feedstock passed to
check_ready_already_done, ends up with a local clone and a fork under `github.com/luhenry` even
if it never reaches Phase 3, and its riscv64 PR notifications get (re-)subscribed to every time
this module checks it. Throttling limits how often `reconcile_tracked` re-checks a given
tracked feedstock; `check_ready_already_done` is not throttled at all (called fresh each triage
run for every "ready" feedstock) so its subscribe calls are the most frequent in the pipeline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from cf_core import conda_forge_yml_check as cfy
from cf_core import state_io

DEFAULT_THROTTLE = timedelta(hours=12)

# Terminal states: once a shadow dependency is confirmed done or permanently platform-blocked,
# there's nothing left to reconcile -- skip it outright rather than throttle-checking it forever.
_TERMINAL_TRACKED_ACTIONS = frozenset({"DONE", "WONTFIX_PLATFORM"})


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_stale(last_checked: Optional[str], now: datetime, throttle: timedelta) -> bool:
    parsed = _parse_iso(last_checked)
    if parsed is None:
        return True
    return (now - parsed) > throttle


def reconcile_tracked(now_iso: str, throttle: timedelta = DEFAULT_THROTTLE) -> list[dict]:
    """Re-verify every non-terminal, non-recently-checked state/_tracked/*.json entry.
    Writes are applied as a side effect via state_io.write_tracked. Returns one report dict per
    entry actually (re-)checked, for the caller to log/summarize."""
    now = _parse_iso(now_iso) or datetime.now(timezone.utc)
    results = []

    for name in state_io.list_tracked_names():
        entry = state_io.read_tracked(name)
        if entry is None:
            continue
        if entry.get("last_action") in _TERMINAL_TRACKED_ACTIONS:
            continue
        if not _is_stale(entry.get("last_checked"), now, throttle):
            continue

        check = cfy.check_feedstock(name)
        previous_action = entry.get("last_action")

        if not check["checked"]:
            results.append({
                "name": name, "changed": False,
                "previous_action": previous_action, "new_action": previous_action,
                "reason": "conda-forge.yml check failed (network)",
            })
            continue

        if check["has_riscv64"]:
            state_io.write_tracked(name, {
                "last_checked": now_iso,
                "last_action": "DONE",
                "riscv64_pr_status": (
                    "confirmed done: conda-forge.yml on main has linux_riscv64 under build_platform"
                ),
            })
            results.append({
                "name": name, "changed": previous_action != "DONE",
                "previous_action": previous_action, "new_action": "DONE",
            })
        else:
            state_io.write_tracked(name, {"last_checked": now_iso})
            results.append({
                "name": name, "changed": False,
                "previous_action": previous_action, "new_action": previous_action,
            })

    return results


def check_ready_already_done(ready_feedstock_names: list[str]) -> list[str]:
    """For feedstocks the migration page currently lists as "ready" (in-PR, deps done), check
    which ones are ALREADY actually done per conda-forge.yml -- catches a feedstock whose PR
    merged between the migration JSON's last refresh and this run, so the (expensive, LLM-driven)
    per-feedstock analysis pipeline doesn't waste a cycle re-analyzing something that's already
    finished. Advisory only -- doesn't write state; the caller decides what to do with the list
    (skip from analysis, log it, etc.), since state/*.json for these is owned by the normal
    triage write path, not by the reconciler."""
    already_done = []
    for name in ready_feedstock_names:
        check = cfy.check_feedstock(name)
        if check["checked"] and check["has_riscv64"]:
            already_done.append(name)
    return already_done
