import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import gh_client


class TestForkAndClone(unittest.TestCase):
    """No real `gh` calls here -- the idempotent short-circuit is tested directly, and the
    actual `gh repo fork --clone` invocation is tested with subprocess.run mocked out (real
    forking needs network + a real GitHub auth session, same reasoning as the "Live" test
    classes elsewhere in this suite that skip when network is unavailable)."""

    def test_already_cloned_is_a_noop(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "libffi-feedstock"))
            with mock.patch.object(gh_client, "_run") as run:
                result = gh_client.fork_and_clone("libffi", root)
            run.assert_not_called()
            self.assertTrue(result["already_cloned"])
            self.assertTrue(result["ok"])
            self.assertEqual(result["path"], os.path.join(root, "libffi-feedstock"))

    def test_not_yet_cloned_invokes_gh_with_documented_command(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "_run", return_value="Cloned fork\n") as run:
                result = gh_client.fork_and_clone("libffi", root)
            self.assertFalse(result["already_cloned"])
            self.assertTrue(result["ok"])
            self.assertIsNone(result["error"])
            (args,), kwargs = run.call_args
            # Exact command documented in CLAUDE.md's Phase-3 procedure -- no variations.
            self.assertEqual(args, [
                "gh", "repo", "fork", "--clone", "--fork-name", "conda-forge-libffi-feedstock",
                "https://github.com/conda-forge/libffi-feedstock", "libffi-feedstock",
            ])
            self.assertEqual(kwargs.get("cwd"), root)

    def test_gh_failure_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "_run", return_value=None):
                result = gh_client.fork_and_clone("libffi", root)
            self.assertFalse(result["ok"])
            self.assertIsNotNone(result["error"])


class TestSubscribe(unittest.TestCase):
    def test_subscribe_reports_success(self):
        with mock.patch.object(gh_client, "_run", return_value="{}\n"):
            self.assertTrue(gh_client.subscribe("conda-forge/libffi-feedstock", 63))

    def test_subscribe_reports_failure(self):
        with mock.patch.object(gh_client, "_run", return_value=None):
            self.assertFalse(gh_client.subscribe("conda-forge/libffi-feedstock", 63))


class TestSubscribeToRiscv64Prs(unittest.TestCase):
    """No real `gh` calls -- search_prs and subscribe are mocked independently so this only
    proves subscribe_to_riscv64_prs's own wiring: it searches OPEN PRs for "riscv64" and
    subscribes to every match, reporting per-PR success/failure rather than raising."""

    def test_searches_open_prs_and_subscribes_to_each_match(self):
        prs = [
            {"number": 63, "title": "riscv64 migration"},
            {"number": 71, "title": "add riscv64 support"},
        ]
        with mock.patch.object(gh_client, "search_prs", return_value=prs) as search, \
             mock.patch.object(gh_client, "subscribe", return_value=True) as sub:
            result = gh_client.subscribe_to_riscv64_prs("conda-forge/libffi-feedstock")
        search.assert_called_once_with("conda-forge/libffi-feedstock", "riscv64", state="open", limit=20)
        self.assertEqual(sub.call_args_list, [
            mock.call("conda-forge/libffi-feedstock", 63),
            mock.call("conda-forge/libffi-feedstock", 71),
        ])
        self.assertEqual(result, [
            {"number": 63, "title": "riscv64 migration", "subscribed": True},
            {"number": 71, "title": "add riscv64 support", "subscribed": True},
        ])

    def test_no_matching_prs_returns_empty_list(self):
        with mock.patch.object(gh_client, "search_prs", return_value=[]), \
             mock.patch.object(gh_client, "subscribe") as sub:
            result = gh_client.subscribe_to_riscv64_prs("conda-forge/libffi-feedstock")
        sub.assert_not_called()
        self.assertEqual(result, [])

    def test_per_pr_subscribe_failure_reported_not_raised(self):
        with mock.patch.object(gh_client, "search_prs", return_value=[{"number": 63, "title": "riscv64"}]), \
             mock.patch.object(gh_client, "subscribe", return_value=False):
            result = gh_client.subscribe_to_riscv64_prs("conda-forge/libffi-feedstock")
        self.assertEqual(result, [{"number": 63, "title": "riscv64", "subscribed": False}])

    def test_custom_limit_passed_through(self):
        with mock.patch.object(gh_client, "search_prs", return_value=[]) as search:
            gh_client.subscribe_to_riscv64_prs("conda-forge/libffi-feedstock", limit=5)
        search.assert_called_once_with("conda-forge/libffi-feedstock", "riscv64", state="open", limit=5)


if __name__ == "__main__":
    unittest.main()
