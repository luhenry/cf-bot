import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import migration_source as ms

DONE_YML = b"build_platform:\n  linux_riscv64: linux_64\n"
NOT_DONE_YML = b"build_platform:\n  linux_aarch64: linux_64\n"


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(path, name, content):
    with open(os.path.join(path, name), "wb") as f:
        f.write(content)


class LocalCloneFixture:
    """Builds an all-local, network-free stand-in for the real "gh repo fork --clone" layout:

      upstream_repo/   -- plays conda-forge/<pkg>-feedstock: has the CURRENT conda-forge.yml on
                           main, plus a synthetic PR ref (refs/pull/<n>/head) with a further change.
      fork_repo/       -- plays Ludovic's fork: cloned from upstream_repo at the base commit only,
                           and deliberately never updated -- proves the code reads from `upstream`,
                           not `origin`/the fork, which is exactly what a stale fork would get wrong.
      clone/           -- plays the local clone gh_client.fork_and_clone would have produced:
                           `origin` -> fork_repo (stale), `upstream` -> upstream_repo (current).
    """

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.upstream = os.path.join(root, "upstream_repo")
        self.fork = os.path.join(root, "fork_repo")
        self.clone = os.path.join(root, "clone")

        os.makedirs(self.upstream)
        _git("init", "-q", "-b", "main", cwd=self.upstream)
        _git("config", "user.email", "test@example.com", cwd=self.upstream)
        _git("config", "user.name", "test", cwd=self.upstream)
        _write(self.upstream, "conda-forge.yml", NOT_DONE_YML)
        _git("add", "conda-forge.yml", cwd=self.upstream)
        _git("commit", "-q", "-m", "base", cwd=self.upstream)

        # Fork the fork BEFORE the "PR"/riscv64 change lands, so it's a stale snapshot.
        subprocess.run(["git", "clone", "-q", self.upstream, self.fork],
                        check=True, capture_output=True, text=True)

        # The PR: a branch off main with the riscv64 change, exposed the way GitHub exposes PRs
        # (a ref outside refs/heads/*), then the branch itself is discarded.
        _git("checkout", "-q", "-b", "pr-5", cwd=self.upstream)
        _write(self.upstream, "conda-forge.yml", DONE_YML)
        _git("commit", "-q", "-am", "add riscv64", cwd=self.upstream)
        pr_sha = subprocess.run(["git", "rev-parse", "pr-5"], cwd=self.upstream,
                                 check=True, capture_output=True, text=True).stdout.strip()
        _git("update-ref", "refs/pull/5/head", pr_sha, cwd=self.upstream)
        _git("checkout", "-q", "main", cwd=self.upstream)
        _git("branch", "-D", "pr-5", cwd=self.upstream)
        # main itself stays on the base (not-done) commit -- only the PR ref has the riscv64 change.

        subprocess.run(["git", "clone", "-q", self.fork, self.clone],
                        check=True, capture_output=True, text=True)
        # `origin` -> fork (stale, from the clone above); add `upstream` -> the real repo, same
        # remote layout `gh repo fork --clone` produces.
        _git("remote", "add", "upstream", self.upstream, cwd=self.clone)
        return self

    def __exit__(self, *exc):
        self.tmp.cleanup()


class TestFetchFileAtRefFromLocalClone(unittest.TestCase):
    def test_reads_from_upstream_not_origin(self):
        with LocalCloneFixture() as fx:
            content = ms.fetch_file_at_ref_from_local_clone(fx.clone, "conda-forge.yml", ref="main")
            self.assertEqual(content, NOT_DONE_YML)  # upstream/main hasn't merged the PR

    def test_missing_remote_returns_none_not_raises(self):
        with LocalCloneFixture() as fx:
            content = ms.fetch_file_at_ref_from_local_clone(fx.clone, "conda-forge.yml", ref="main", remote="nope")
            self.assertIsNone(content)

    def test_missing_clone_dir_returns_none(self):
        content = ms.fetch_file_at_ref_from_local_clone("/nonexistent/path", "conda-forge.yml")
        self.assertIsNone(content)


class TestDiffPrFilesInLocalClone(unittest.TestCase):
    def test_diffs_pr_ref_against_upstream_main(self):
        with LocalCloneFixture() as fx:
            diff = ms.diff_pr_files_in_local_clone(fx.clone, 5, ["conda-forge.yml"])
            self.assertIsNotNone(diff)
            self.assertIn("linux_riscv64", diff)

    def test_no_local_branch_ref_left_behind(self):
        with LocalCloneFixture() as fx:
            ms.diff_pr_files_in_local_clone(fx.clone, 5, ["conda-forge.yml"])
            branches = subprocess.run(["git", "branch"], cwd=fx.clone,
                                       check=True, capture_output=True, text=True).stdout
            self.assertNotIn("pr-5", branches)

    def test_missing_pr_ref_returns_none(self):
        with LocalCloneFixture() as fx:
            diff = ms.diff_pr_files_in_local_clone(fx.clone, 9999, ["conda-forge.yml"])
            self.assertIsNone(diff)


class TestConventions(unittest.TestCase):
    def test_local_clone_path_uses_default_root(self):
        with mock.patch.object(ms, "default_conda_forge_root", return_value="/x"):
            self.assertEqual(ms.local_clone_path("libffi"), "/x/libffi-feedstock")

    def test_local_clone_path_explicit_root_overrides_default(self):
        with mock.patch.object(ms, "default_conda_forge_root", side_effect=AssertionError("should not be called")):
            self.assertEqual(ms.local_clone_path("libffi", "/y"), "/y/libffi-feedstock")

    def test_default_conda_forge_root_is_parent_of_cwd(self):
        self.assertEqual(ms.default_conda_forge_root(), os.path.dirname(os.getcwd()))


if __name__ == "__main__":
    unittest.main()
