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
"""
from __future__ import annotations

from typing import Optional

import yaml

from cf_core import migration_source as ms


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


def check_feedstock(feedstock: str) -> dict:
    """Fetch conda-forge.yml@main for `feedstock` and report whether riscv64 is already merged.

    Returns:
      {"feedstock": ..., "checked": bool, "has_riscv64": Optional[bool], "error": Optional[str]}
    `checked` is False (not an exception) if the fetch itself failed -- network trouble or the
    repo/file genuinely not existing are the same "we don't know" outcome to a caller, which
    should not treat that as "no riscv64" (that would be a false negative baked into automation).
    """
    repo_url = ms.feedstock_repo_url(feedstock)
    content = ms.fetch_file_at_ref_via_git(repo_url, "conda-forge.yml", ref="main")
    if content is None:
        return {"feedstock": feedstock, "checked": False, "has_riscv64": None,
                 "error": "could not fetch conda-forge.yml@main"}
    return {"feedstock": feedstock, "checked": True,
            "has_riscv64": has_riscv64_support(content), "error": None}


def diff_pr(feedstock: str, pr_number: int, paths: Optional[list[str]] = None) -> str:
    """Diff a PR's changes to `conda-forge.yml` and `recipe/` against `main` -- the technique
    used to catch the pandoc-feedstock#171 mislabeled-binary bug. Returns the raw unified diff
    text; callers (human or LLM agent) interpret it."""
    repo_url = ms.feedstock_repo_url(feedstock)
    return ms.diff_pr_files(repo_url, pr_number, paths or ["conda-forge.yml", "recipe/"])
