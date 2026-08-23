import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import cli


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


if __name__ == "__main__":
    unittest.main()
