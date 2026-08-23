"""
cf_core.conda_forge_yml_check — the authoritative "does this feedstock already have riscv64
support" check.

Discovered this session: the migration JSON's `done`/`in-pr` sets, and even a PR's own green CI,
are not fully trustworthy on their own -- pandoc-feedstock#171 had riscv64 CI passing while
silently repackaging an x86-64 binary as riscv64 (a `# [linux and not aarch64]` source-URL
selector that also matched riscv64). The one check that can't be fooled that way: read
`conda-forge.yml` at the repo root on the `main` branch and look for `linux_riscv64` under
`build_platform`. If it's not there, riscv64 support has not actually been merged, whatever the
migration page or a PR's status checks say.

Before this module existed, this was only documented shell commands in CLAUDE.md that a human
had to remember to run by hand -- promoted here to a tested, reusable function, and wired into
cf_core.reconciler so it runs automatically instead of only when someone happens to ask.

Both `check_feedstock` and `diff_pr` read from a local clone (created by
`cf_core.gh_client.fork_and_clone` during Phase 1 Verify, or by hand via CLAUDE.md's Phase-3
procedure) -- see `cf_core.migration_source`'s module docstring for how the `upstream` remote
convention works. If no local clone exists yet for a given feedstock (most shadow dependencies,
or a feedstock's very first check), this module forks and clones it itself via
`gh_client.fork_and_clone` -- i.e. the real `gh repo fork --clone`, never a disposable `git
clone`/`git init` of the feedstock as a substitute. One consequence worth knowing: every
feedstock this module is ever asked to check ends up with a durable local clone under
`conda-forge/<pkg>-feedstock` and a fork under `github.com/luhenry` -- including CUDA
`WONTFIX_PLATFORM` feedstocks and shadow dependencies, not just feedstocks that make it to
Phase 3. That's an accepted, deliberate trade-off (one tool for "get me this feedstock locally",
no second ad hoc technique to keep in sync), not an oversight.

`check_feedstock` additionally calls `gh_client.subscribe_to_riscv64_prs` every time it runs
(whether the local clone was just created or already existed) -- this is the mechanism behind
"make sure to subscribe to any PR notifications getting riscv64 contributions": a broad
`gh pr list --search riscv64` on the feedstock, not just the one bot PR / one best PR the JS
workflow layer happens to already know a number for. It runs during Phase 1 Verify, before any
PR has even been looked at yet, and again on every subsequent Verify pass for that feedstock, so
a riscv64 PR opened after the initial fork still gets picked up on the next run rather than only
at fork time.
"""
from __future__ import annotations

import os
from typing import Optional

import yaml

from cf_core import gh_client
from cf_core import migration_source as ms


def _ensure_local_clone(feedstock: str, conda_forge_root: Optional[str]) -> Optional[str]:
    """Return the feedstock's local clone path, forking+cloning it via `gh_client.fork_and_clone`
    (real `gh repo fork --clone`, not a disposable `git clone`) first if it doesn't already
    exist. Returns None if `gh` itself fails (not installed, unauthenticated, rate-limited, no
    network) -- callers treat that the same as any other fetch failure (checked=False), not as
    "no riscv64"."""
    clone_path = ms.local_clone_path(feedstock, conda_forge_root)
    if os.path.isdir(clone_path):
        return clone_path
    result = gh_client.fork_and_clone(feedstock, conda_forge_root)
    return clone_path if result.get("ok") else None


def has_riscv64_support(conda_forge_yml_content: bytes) -> bool:
    """True if `build_platform` has a `linux_riscv64` key. Falls back to a plain substring
    check if the YAML fails to parse (conda-forge.yml is usually plain, boring YAML, but this
    keeps the check from raising on something unexpected -- absence of evidence isn't proof of
    absence, so a parse failure is treated as informative-but-not-authoritative and callers
    should treat `parsed: False` as "needs a human look", not "no riscv64")."""
    try:
        data = yaml.safe_load(conda_forge_yml_content) or {}
    except yaml.YAMLError:
        return "linux_riscv64" in conda_forge_yml_content.decode("utf-8", errors="replace")
    build_platform = data.get("build_platform") or {}
    if not isinstance(build_platform, dict):
        return False
    return "linux_riscv64" in build_platform


def check_feedstock(feedstock: str, conda_forge_root: Optional[str] = None) -> dict:
    """Fetch conda-forge.yml@main for `feedstock` and report whether riscv64 is already merged.
    Reads from the feedstock's local clone, forking+cloning it via `gh` first if it doesn't
    already exist (see module docstring) -- never a disposable `git clone` of the feedstock. Also
    subscribes to every open riscv64-related PR on the feedstock (see
    `gh_client.subscribe_to_riscv64_prs`) -- this is Phase 1 Verify's one deterministic touchpoint
    for both "get this feedstock locally" and "make sure we hear about riscv64 PR activity on
    it," so callers (the CLI, analyze_feedstock.js's Verify phase) don't need a separate step.

    Returns:
      {"feedstock": ..., "checked": bool, "has_riscv64": Optional[bool], "error": Optional[str],
       "source": Optional[str], "riscv64_pr_subscriptions": list[dict]}
      # "source" is "local_clone" or None; "riscv64_pr_subscriptions" is
      # gh_client.subscribe_to_riscv64_prs's return value, or [] if there was no usable clone to
      # subscribe from at all.
    `checked` is False (not an exception) if the fetch itself failed -- network trouble, `gh`
    being unavailable/unauthenticated, or the repo/file genuinely not existing are all the same
    "we don't know" outcome to a caller, which should not treat that as "no riscv64" (that would
    be a false negative baked into automation).
    """
    content = None
    source = None
    subscriptions = []

    clone_path = _ensure_local_clone(feedstock, conda_forge_root)
    if clone_path:
        content = ms.fetch_file_at_ref_from_local_clone(clone_path, "conda-forge.yml", ref="main")
        if content is not None:
            source = "local_clone"
        subscriptions = gh_client.subscribe_to_riscv64_prs(f"conda-forge/{feedstock}-feedstock")

    if content is None:
        return {"feedstock": feedstock, "checked": False, "has_riscv64": None,
                "error": "could not fetch conda-forge.yml@main (no usable local clone, "
                         "and `gh repo fork --clone` did not succeed)", "source": None,
                "riscv64_pr_subscriptions": subscriptions}
    return {"feedstock": feedstock, "checked": True,
            "has_riscv64": has_riscv64_support(content), "error": None, "source": source,
            "riscv64_pr_subscriptions": subscriptions}


def diff_pr(
    feedstock: str, pr_number: int, paths: Optional[list[str]] = None,
    conda_forge_root: Optional[str] = None,
) -> str:
    """Diff a PR's changes to `conda-forge.yml` and `recipe/` against `main` -- the technique
    used to catch the pandoc-feedstock#171 mislabeled-binary bug. Returns the raw unified diff
    text; callers (human or LLM agent) interpret it. Reads from the feedstock's local clone,
    forking+cloning it via `gh` first if it doesn't already exist (see module docstring) --
    never a disposable `git clone` of the feedstock.

    Raises RuntimeError if no usable local clone could be obtained/read -- unlike
    check_feedstock's "checked: False", there's no natural non-exceptional value to return for a
    diff, so callers (the CLI, an LLM agent relay) see a real failure instead of silently getting
    back an empty string that could be misread as "no changes"."""
    paths = paths or ["conda-forge.yml", "recipe/"]

    clone_path = _ensure_local_clone(feedstock, conda_forge_root)
    if clone_path:
        diff = ms.diff_pr_files_in_local_clone(clone_path, pr_number, paths)
        if diff is not None:
            return diff

    raise RuntimeError(
        f"could not diff PR #{pr_number} for {feedstock}: no usable local clone (`gh repo fork "
        f"--clone` did not succeed, or the local diff itself failed -- e.g. bad PR number)"
    )
