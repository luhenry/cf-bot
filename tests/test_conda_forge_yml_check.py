import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import conda_forge_yml_check as cfy
from cf_core import gh_client
from cf_core import migration_source as ms

DONE_YML = b"""
build_platform:
  linux_riscv64: linux_64
  linux_aarch64: linux_64
test: native_and_emulated
"""

NOT_DONE_YML = b"""
build_platform:
  linux_aarch64: linux_64
  osx_arm64: osx_64
"""

NO_BUILD_PLATFORM_YML = b"""
provider:
  linux64: azure
"""


class TestHasRiscv64Support(unittest.TestCase):
    def test_detects_present(self):
        self.assertTrue(cfy.has_riscv64_support(DONE_YML))

    def test_detects_absent(self):
        self.assertFalse(cfy.has_riscv64_support(NOT_DONE_YML))

    def test_missing_build_platform_key(self):
        self.assertFalse(cfy.has_riscv64_support(NO_BUILD_PLATFORM_YML))

    def test_empty_content(self):
        self.assertFalse(cfy.has_riscv64_support(b""))

    def test_malformed_yaml_falls_back_to_substring(self):
        # deliberately broken YAML (unbalanced brackets) that still mentions linux_riscv64
        broken = b"build_platform: [linux_riscv64: linux_64"
        self.assertTrue(cfy.has_riscv64_support(broken))


class TestCheckFeedstockSourcePreference(unittest.TestCase):
    """No network, no real `gh`/git calls -- proves check_feedstock/diff_pr read from an existing
    local clone directly when one is already there, and otherwise call gh_client.fork_and_clone
    to create one (never a disposable `git clone` of the feedstock) before reading from it. The
    actual git-fetch/show/diff mechanics of the local-clone read functions are covered against
    real repos in tests/test_migration_source.py; gh_client.fork_and_clone's and
    subscribe_to_riscv64_prs's own behavior is covered in tests/test_gh_client.py."""

    def test_reads_directly_when_clone_already_exists(self):
        with tempfile.TemporaryDirectory() as root:
            clone_path = os.path.join(root, "libffi-feedstock")
            os.mkdir(clone_path)
            with mock.patch.object(ms, "fetch_file_at_ref_from_local_clone", return_value=DONE_YML) as local, \
                 mock.patch.object(gh_client, "fork_and_clone") as fork, \
                 mock.patch.object(gh_client, "subscribe_to_riscv64_prs", return_value=[]) as sub:
                result = cfy.check_feedstock("libffi", conda_forge_root=root)
            fork.assert_not_called()
            local.assert_called_once()
            sub.assert_called_once_with("conda-forge/libffi-feedstock")
            self.assertEqual(result["source"], "local_clone")
            self.assertTrue(result["has_riscv64"])
            self.assertEqual(result["riscv64_pr_subscriptions"], [])

    def test_forks_and_clones_when_no_local_clone_yet(self):
        with tempfile.TemporaryDirectory() as root:
            clone_path = os.path.join(root, "libffi-feedstock")

            def fake_fork_and_clone(feedstock, conda_forge_root):
                os.mkdir(clone_path)  # simulate `gh repo fork --clone` creating the directory
                return {"feedstock": feedstock, "path": clone_path, "already_cloned": False,
                        "ok": True, "error": None}

            with mock.patch.object(gh_client, "fork_and_clone", side_effect=fake_fork_and_clone) as fork, \
                 mock.patch.object(ms, "fetch_file_at_ref_from_local_clone", return_value=NOT_DONE_YML), \
                 mock.patch.object(gh_client, "subscribe_to_riscv64_prs", return_value=[]):
                result = cfy.check_feedstock("libffi", conda_forge_root=root)
            fork.assert_called_once_with("libffi", root)
            self.assertEqual(result["source"], "local_clone")
            self.assertFalse(result["has_riscv64"])

    def test_gh_fork_failure_reports_unchecked_not_an_exception(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "fork_and_clone",
                                    return_value={"ok": False, "error": "gh not authenticated"}), \
                 mock.patch.object(gh_client, "subscribe_to_riscv64_prs") as sub:
                result = cfy.check_feedstock("libffi", conda_forge_root=root)
            sub.assert_not_called()  # no usable clone/repo access -- nothing to subscribe with
            self.assertFalse(result["checked"])
            self.assertIsNone(result["has_riscv64"])
            self.assertIsNone(result["source"])
            self.assertEqual(result["riscv64_pr_subscriptions"], [])

    def test_subscribes_to_riscv64_prs_even_when_yml_read_fails(self):
        # A usable clone but a failed conda-forge.yml read shouldn't skip subscribing -- the two
        # are independent (subscription only needs the repo to exist, not a successful read).
        with tempfile.TemporaryDirectory() as root:
            clone_path = os.path.join(root, "libffi-feedstock")
            os.mkdir(clone_path)
            with mock.patch.object(ms, "fetch_file_at_ref_from_local_clone", return_value=None), \
                 mock.patch.object(gh_client, "subscribe_to_riscv64_prs",
                                    return_value=[{"number": 63, "title": "riscv64", "subscribed": True}]) as sub:
                result = cfy.check_feedstock("libffi", conda_forge_root=root)
            sub.assert_called_once_with("conda-forge/libffi-feedstock")
            self.assertFalse(result["checked"])
            self.assertEqual(result["riscv64_pr_subscriptions"], [{"number": 63, "title": "riscv64", "subscribed": True}])

    def test_diff_pr_reads_directly_when_clone_already_exists(self):
        with tempfile.TemporaryDirectory() as root:
            clone_path = os.path.join(root, "libffi-feedstock")
            os.mkdir(clone_path)
            with mock.patch.object(ms, "diff_pr_files_in_local_clone", return_value="a diff") as local, \
                 mock.patch.object(gh_client, "fork_and_clone") as fork:
                result = cfy.diff_pr("libffi", 63, conda_forge_root=root)
            fork.assert_not_called()
            local.assert_called_once()
            self.assertEqual(result, "a diff")

    def test_diff_pr_forks_and_clones_when_no_local_clone_yet(self):
        with tempfile.TemporaryDirectory() as root:
            clone_path = os.path.join(root, "libffi-feedstock")

            def fake_fork_and_clone(feedstock, conda_forge_root):
                os.mkdir(clone_path)
                return {"ok": True}

            with mock.patch.object(gh_client, "fork_and_clone", side_effect=fake_fork_and_clone) as fork, \
                 mock.patch.object(ms, "diff_pr_files_in_local_clone", return_value="a diff"):
                result = cfy.diff_pr("libffi", 63, conda_forge_root=root)
            fork.assert_called_once()
            self.assertEqual(result, "a diff")

    def test_diff_pr_raises_when_no_usable_clone(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "fork_and_clone", return_value={"ok": False}):
                with self.assertRaises(RuntimeError):
                    cfy.diff_pr("libffi", 63, conda_forge_root=root)


class TestCheckFeedstockLive(unittest.TestCase):
    """Hits the real `gh` CLI -- same live checks performed by hand this session for zstd
    (confirmed done) and pandoc (confirmed not yet done). Skipped if network/gh is unavailable.

    IMPORTANT: check_feedstock now forks+clones via `gh repo fork --clone` when no local clone
    exists (see cf_core/conda_forge_yml_check.py's module docstring) -- so this class, unlike the
    rest of the suite, has a REAL side effect: on a machine with `gh` installed and authenticated
    (e.g. the actual cf-bot execution environment, NOT this repo's own CI/dev sandboxes), running
    it creates real forks under the authenticated account and real local clones on disk. Gated
    behind an explicit opt-in env var so `python3 -m unittest discover -s tests` (CLAUDE.md's
    documented default test command) never does this by accident."""

    def setUp(self):
        if os.environ.get("CF_BOT_LIVE_FORK_TESTS") != "1":
            self.skipTest(
                "skipped by default -- forks real repos via `gh`. Set "
                "CF_BOT_LIVE_FORK_TESTS=1 to opt in explicitly."
            )
        # Real forks/clones still happen (that's the point of this class), but into a throwaway
        # directory -- not Ludovic's actual conda-forge/ checkout -- so a stray opted-in run
        # doesn't leave clutter in his real working tree.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_zstd_confirmed_done(self):
        result = cfy.check_feedstock("zstd", conda_forge_root=self._tmp.name)
        if not result["checked"]:
            self.skipTest("network/gh unavailable for live check")
        self.assertTrue(result["has_riscv64"])

    def test_pandoc_not_yet_done(self):
        result = cfy.check_feedstock("pandoc", conda_forge_root=self._tmp.name)
        if not result["checked"]:
            self.skipTest("network/gh unavailable for live check")
        self.assertFalse(result["has_riscv64"])


if __name__ == "__main__":
    unittest.main()
