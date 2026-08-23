import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import cli, gh_client
from cf_core import conda_forge_yml_check as cfy


def run_cli(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(argv)
    return json.loads(buf.getvalue())


class TestCliNetworkFree(unittest.TestCase):
    """Only exercises subcommands that don't need the network -- graph/verify/reconcile are
    covered live in the other test modules and via the manual parity check."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_policy_check_cuda(self):
        result = run_cli(["policy", "check-cuda", "nccl"])
        self.assertTrue(result["is_cuda_wontfix"])

    def test_policy_check_v1_bundle(self):
        result = run_cli(["policy", "check-v1-bundle", "--title", "Add riscv64, migrate to v1"])
        self.assertTrue(result["pr_is_v1_migration"])

    def test_policy_check_commit_message(self):
        result = run_cli(["policy", "check-commit-message", "--message", "clean commit"])
        self.assertTrue(result["clean"])
        dirty = run_cli(["policy", "check-commit-message", "--message", "Co-Authored-By: Claude"])
        self.assertFalse(dirty["clean"])

    def test_state_write_then_read_roundtrip(self):
        run_cli(["state", "write", "libffi", "--json", json.dumps({"last_action": "NUDGE_MERGE"})])
        result = run_cli(["state", "read", "libffi"])
        self.assertEqual(result["last_action"], "NUDGE_MERGE")
        self.assertEqual(result["feedstock"], "libffi")

    def test_state_write_tracked(self):
        run_cli(["state", "write", "zstd", "--tracked", "--json", json.dumps({"last_action": "DONE"})])
        result = run_cli(["state", "read", "zstd", "--tracked"])
        self.assertEqual(result["last_action"], "DONE")

    def test_state_write_invalid_action_exits_nonzero(self):
        with self.assertRaises(Exception):
            run_cli(["state", "write", "foo", "--json", json.dumps({"last_action": "BOGUS"})])

    def test_gh_fork_clone_already_cloned(self):
        # self._tmp is cwd; --conda-forge-root defaults to its parent, so create the sibling
        # clone dir there directly rather than relying on the default resolving anywhere useful.
        conda_forge_root = os.path.dirname(self._tmp.name)
        target = os.path.join(conda_forge_root, "libffi-feedstock")
        created = not os.path.isdir(target)
        if created:
            os.mkdir(target)
        try:
            result = run_cli(["gh", "fork-clone", "libffi", "--conda-forge-root", conda_forge_root])
            self.assertTrue(result["already_cloned"])
            self.assertTrue(result["ok"])
        finally:
            if created:
                os.rmdir(target)

    def test_gh_fork_clone_invokes_gh_client(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "_run", return_value="ok\n"):
                result = run_cli(["gh", "fork-clone", "libffi", "--conda-forge-root", root])
            self.assertFalse(result["already_cloned"])
            self.assertTrue(result["ok"])

    def test_gh_subscribe(self):
        with mock.patch.object(gh_client, "_run", return_value="{}\n"):
            result = run_cli(["gh", "subscribe", "conda-forge/libffi-feedstock", "63"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["pr_number"], 63)

    def test_gh_subscribe_riscv64(self):
        with mock.patch.object(gh_client, "search_prs", return_value=[{"number": 63, "title": "riscv64"}]), \
             mock.patch.object(gh_client, "subscribe", return_value=True):
            result = run_cli(["gh", "subscribe-riscv64", "conda-forge/libffi-feedstock"])
        self.assertEqual(result["repo"], "conda-forge/libffi-feedstock")
        self.assertEqual(result["subscriptions"], [{"number": 63, "title": "riscv64", "subscribed": True}])

    def test_verify_feedstock_forks_clones_and_subscribes(self):
        # End-to-end through the CLI: verify feedstock -> conda_forge_yml_check.check_feedstock
        # -> gh_client.fork_and_clone + subscribe_to_riscv64_prs, with only the `gh`-touching
        # leaf calls mocked.
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(gh_client, "_run", return_value="ok\n"), \
                 mock.patch.object(cfy.ms, "fetch_file_at_ref_from_local_clone", return_value=b"build_platform:\n  linux_riscv64: x\n"), \
                 mock.patch.object(gh_client, "search_prs", return_value=[{"number": 5, "title": "riscv64"}]), \
                 mock.patch.object(gh_client, "subscribe", return_value=True):
                result = run_cli(["verify", "feedstock", "libffi", "--conda-forge-root", root])
        self.assertTrue(result["conda_forge_yml"]["checked"])
        self.assertTrue(result["conda_forge_yml"]["has_riscv64"])
        self.assertEqual(
            result["conda_forge_yml"]["riscv64_pr_subscriptions"],
            [{"number": 5, "title": "riscv64", "subscribed": True}],
        )


if __name__ == "__main__":
    unittest.main()
