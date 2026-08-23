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

Since cf_core.gh_client.fork_and_clone gave Phase 1-2 a real, persistent local clone of every
feedstock it analyzes (at `local_clone_path(feedstock)`), the two ephemeral-tmpdir techniques
above are no longer the only way to get a file or a diff. When a local clone already exists,
`fetch_file_at_ref_from_local_clone` / `diff_pr_files_in_local_clone` reuse it directly -- a
cheap incremental `git fetch` against the `upstream` remote that `gh repo fork --clone` sets up
(the local clone's `origin` is Ludovic's fork, which does NOT auto-track conda-forge/main; the
parent repo `gh` adds as `upstream` is what has the real, current `main`) -- instead of paying
for a brand-new tmpdir clone every time. `cf_core.conda_forge_yml_check` tries the local-clone
path first and falls back to the ephemeral tmpdir technique, which stays necessary whenever no
local clone exists yet: a feedstock's first-ever check (before Setup has run), a shadow
dependency (which never goes through Setup at all), or a one-off check of a feedstock outside
this repo's directory layout.
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


def default_conda_forge_root() -> str:
    """The `conda-forge/` checkout root, by the convention documented in CLAUDE.md's
    "Contributing to a feedstock" section: cf_core is invoked with cwd == the `.bot` checkout,
    which lives directly inside `conda-forge/`, alongside every feedstock clone. Centralized here
    so gh_client.fork_and_clone (which CREATES clones at this path) and
    conda_forge_yml_check/migration_source (which LOOK for clones at this path) can't drift
    apart -- both resolve the same convention through this one function."""
    return os.path.dirname(os.getcwd())


def local_clone_path(feedstock: str, conda_forge_root: Optional[str] = None) -> str:
    """Where `<feedstock>-feedstock` lives locally if `cf_core.gh_client.fork_and_clone` (or the
    equivalent manual `gh repo fork --clone` from CLAUDE.md's Phase-3 procedure) has already been
    run for it. Does not check that the path actually exists -- callers do that themselves (they
    usually need to react differently to "no local clone" than to "clone exists but is broken")."""
    root = conda_forge_root or default_conda_forge_root()
    return os.path.join(root, f"{feedstock}-feedstock")


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


def fetch_file_at_ref_from_local_clone(
    clone_path: str, path: str, ref: str = "main", remote: str = "upstream",
) -> Optional[bytes]:
    """Like `fetch_file_at_ref_via_git`, but reuses an existing local clone at `clone_path`
    (created by `gh_client.fork_and_clone`) instead of a fresh tmpdir clone: one cheap
    incremental `git fetch` against `remote` (default "upstream" -- the parent-repo remote
    `gh repo fork --clone` sets up; the clone's `origin` is the fork, which does NOT track
    conda-forge's `main` on its own) followed by `git show <fetched-sha>:<path>`. Reads the
    fetched commit via FETCH_HEAD rather than `<remote>/<ref>` so this doesn't depend on whether
    the remote's configured fetch refspec happens to update that particular tracking ref.

    Returns None (not an exception) on any failure -- missing clone, missing remote, network
    trouble, or the path not existing at that ref -- so callers fall through to the ephemeral
    tmpdir technique exactly like the HTTPS-first path already does for fetch_url.
    """
    try:
        subprocess.run(
            ["git", "fetch", "-q", remote, ref],
            cwd=clone_path, check=True, capture_output=True, text=True, timeout=30,
        )
        result = subprocess.run(
            ["git", "show", f"FETCH_HEAD:{path}"],
            cwd=clone_path, check=True, capture_output=True, timeout=15,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def diff_pr_files_in_local_clone(
    clone_path: str, pr_number: int, paths: list[str], remote: str = "upstream",
) -> Optional[str]:
    """Like `diff_pr_files`, but reuses an existing local clone at `clone_path` instead of a
    fresh tmpdir + `git init`: two incremental `git fetch`es against `remote` (default
    "upstream", same reasoning as fetch_file_at_ref_from_local_clone), each resolved to a commit
    SHA via FETCH_HEAD right after its own fetch (fetching the PR ref would otherwise overwrite
    FETCH_HEAD before the diff runs) -- no local branch ref is created, so repeated checks don't
    accumulate stale refs in what is otherwise Ludovic's persistent working clone.

    Returns None (not an exception) on any failure, so callers fall through to diff_pr_files.
    """
    def _fetch_sha(refspec: str) -> str:
        subprocess.run(
            ["git", "fetch", "-q", remote, refspec],
            cwd=clone_path, check=True, capture_output=True, text=True, timeout=60,
        )
        rev = subprocess.run(
            ["git", "rev-parse", "FETCH_HEAD"],
            cwd=clone_path, check=True, capture_output=True, text=True, timeout=10,
        )
        return rev.stdout.strip()

    try:
        base_sha = _fetch_sha("main")
        head_sha = _fetch_sha(f"refs/pull/{pr_number}/head")
        result = subprocess.run(
            ["git", "diff", base_sha, head_sha, "--", *paths],
            cwd=clone_path, check=True, capture_output=True, text=True, timeout=30,
        )
        return result.stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None


def fetch_migration_json() -> dict:
    """Fetch the riscv64 migration status JSON (the file `cf_core.graph` builds the dependency
    graph from). HTTPS first, git-clone fallback if blocked."""
    content = fetch_url(MIGRATION_RAW_URL)
    if content is None:
        content = fetch_file_at_ref_via_git(MIGRATION_REPO, MIGRATION_REPO_PATH, ref="main")
    if content is None:
        raise RuntimeError("could not fetch migration JSON via HTTPS or git-clone fallback")
    return json.loads(content)
