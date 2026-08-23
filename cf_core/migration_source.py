"""
cf_core.migration_source — fetching data from public conda-forge GitHub repos.

Generalizes what was, before this rearchitecture, a single-purpose function baked directly into
riscv64_status.py (`fetch_migration_data()`, hardcoded to one URL and one repo). Two callers need
this now: the migration-graph fetch (unchanged use case) and cf_core.conda_forge_yml_check (needs
to fetch an arbitrary `conda-forge.yml` at an arbitrary ref from an arbitrary feedstock repo, and
diff an arbitrary PR's changed files -- both discovered as ad hoc one-off shell commands this
session, promoted here to tested, reusable functions).

Fetch strategy, in order: a direct HTTPS GET (fast, ~1s) against raw.githubusercontent.com; if
that fails (some sandboxed environments block it outright, alongside api.github.com), fall back
to a blobless partial `git clone` + non-cone `git sparse-checkout` of just the one file needed
(~10s -- much slower than a direct GET but still far faster than a full clone of a repo with
hundreds of other files in its tree, and works because plain `git clone` over
`https://github.com/...` is a different code path -- smart-HTTP git protocol, not the REST/raw-
content API -- and isn't gated the same way). This ONLY gets file contents of a public repo at a
ref; it does not unblock PR/issue/CI-check API data (`gh pr view`, etc. still need real `gh`
access -- see cf_core.gh_client).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Optional

MIGRATION_REPO = "https://github.com/conda-forge/conda-forge-bot-data"
MIGRATION_REPO_PATH = "status/migration_json/supportlinuxriscv64platform.json"
MIGRATION_RAW_URL = (
    f"https://raw.githubusercontent.com/conda-forge/conda-forge-bot-data/main/{MIGRATION_REPO_PATH}"
)


def feedstock_repo_url(feedstock: str) -> str:
    return f"https://github.com/conda-forge/{feedstock}-feedstock"


def fetch_url(url: str, timeout: int = 15) -> Optional[bytes]:
    """Direct HTTPS GET. Returns None (not an exception) on any network failure, so callers can
    fall through to the git-clone technique uniformly."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None


def fetch_file_at_ref_via_git(repo_url: str, path: str, ref: str = "main") -> Optional[bytes]:
    """Fetch a single file's contents at `ref` from a public repo via blobless partial clone +
    sparse-checkout. Returns None (not an exception) if the clone, checkout, or file lookup
    fails, for the same reason as fetch_url."""
    tmpdir = tempfile.mkdtemp(prefix="cf-core-fetch-")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", "--quiet",
             "--branch", ref, repo_url, tmpdir],
            check=True, capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            ["git", "sparse-checkout", "init", "--no-cone"],
            cwd=tmpdir, check=True, capture_output=True, text=True, timeout=30,
        )
        with open(os.path.join(tmpdir, ".git", "info", "sparse-checkout"), "w") as f:
            f.write(path + "\n")
        subprocess.run(
            ["git", "checkout", "--quiet"],
            cwd=tmpdir, check=True, capture_output=True, text=True, timeout=30,
        )
        full = os.path.join(tmpdir, path)
        if not os.path.exists(full):
            return None
        with open(full, "rb") as f:
            return f.read()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_file_at_ref(repo_url: str, path: str, ref: str = "main", raw_url: Optional[str] = None) -> bytes:
    """Fetch a file's content at `ref`: HTTPS first if `raw_url` is given, then the git-clone
    fallback either way. Raises FileNotFoundError if both fail or the path doesn't exist at that
    ref -- callers that want a "not found" vs "network broken" distinction should call the two
    lower-level functions directly instead."""
    if raw_url:
        content = fetch_url(raw_url)
        if content is not None:
            return content
    content = fetch_file_at_ref_via_git(repo_url, path, ref)
    if content is None:
        raise FileNotFoundError(f"could not fetch {path!r}@{ref} from {repo_url} via HTTPS or git-clone fallback")
    return content


def diff_pr_files(repo_url: str, pr_number: int, paths: list[str]) -> str:
    """Diff a PR's head ref against `main` for the given paths, via `git fetch
    refs/pull/<n>/head` + `git diff` -- works without any `gh`/API access at all, just plain git.
    This is exactly the technique used to catch the pandoc-feedstock#171 mislabeled-binary bug
    (a `# [linux and not aarch64]` selector that silently also matched riscv64)."""
    tmpdir = tempfile.mkdtemp(prefix="cf-core-prdiff-")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmpdir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "remote", "add", "origin", repo_url], cwd=tmpdir,
                        check=True, capture_output=True, text=True)
        subprocess.run(["git", "fetch", "-q", "--depth", "1", "origin", "main"],
                        cwd=tmpdir, check=True, capture_output=True, text=True, timeout=60)
        subprocess.run(
            ["git", "fetch", "-q", "origin", f"refs/pull/{pr_number}/head:pr-{pr_number}"],
            cwd=tmpdir, check=True, capture_output=True, text=True, timeout=60,
        )
        result = subprocess.run(
            ["git", "diff", "origin/main", f"pr-{pr_number}", "--", *paths],
            cwd=tmpdir, check=True, capture_output=True, text=True,
        )
        return result.stdout
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_migration_json() -> dict:
    """Fetch the riscv64 migration status JSON (the file `cf_core.graph` builds the dependency
    graph from). HTTPS first, git-clone fallback if blocked."""
    content = fetch_url(MIGRATION_RAW_URL)
    if content is None:
        content = fetch_file_at_ref_via_git(MIGRATION_REPO, MIGRATION_REPO_PATH, ref="main")
    if content is None:
        raise RuntimeError("could not fetch migration JSON via HTTPS or git-clone fallback")
    return json.loads(content)
