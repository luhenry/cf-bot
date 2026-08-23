import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import reconciler
from cf_core import state_io as sio


class TestReconcileTracked(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._cwd)
        self._tmp.cleanup()

    def test_flips_stale_entry_to_done(self):
        # Reproduce the exact zstd case from this session: stuck at NEEDS_VERIFICATION with an
        # old last_checked, but conda-forge.yml actually already has riscv64.
        sio.write_tracked("zstd", {
            "discovered_from": ["python"],
            "first_seen": "2026-08-21T12:00:00+00:00",
            "last_checked": "2026-08-21T12:00:00+00:00",
            "last_action": "NEEDS_VERIFICATION",
            "depth_to_ci_setup": 1,
        })

        with mock.patch("cf_core.reconciler.cfy.check_feedstock") as m:
            m.return_value = {"feedstock": "zstd", "checked": True, "has_riscv64": True, "error": None}
            results = reconciler.reconcile_tracked(now_iso="2026-08-23T00:00:00+00:00")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "zstd")
        self.assertTrue(results[0]["changed"])
        self.assertEqual(results[0]["new_action"], "DONE")

        updated = sio.read_tracked("zstd")
        self.assertEqual(updated["last_action"], "DONE")
        # fields not touched by the reconciler must survive
        self.assertEqual(updated["depth_to_ci_setup"], 1)
        self.assertEqual(updated["discovered_from"], ["python"])

    def test_terminal_entries_skipped_without_network_call(self):
        sio.write_tracked("cuda-nvml-dev", {"last_action": "WONTFIX_PLATFORM",
                                              "last_checked": "2020-01-01T00:00:00+00:00"})
        with mock.patch("cf_core.reconciler.cfy.check_feedstock") as m:
            results = reconciler.reconcile_tracked(now_iso="2026-08-23T00:00:00+00:00")
            m.assert_not_called()
        self.assertEqual(results, [])

    def test_recently_checked_entries_throttled(self):
        sio.write_tracked("expat", {"last_action": "NEEDS_MINIMAL_PR",
                                      "last_checked": "2026-08-22T23:00:00+00:00"})
        with mock.patch("cf_core.reconciler.cfy.check_feedstock") as m:
            results = reconciler.reconcile_tracked(
                now_iso="2026-08-23T00:00:00+00:00",
                throttle=reconciler.DEFAULT_THROTTLE,
            )
            m.assert_not_called()
        self.assertEqual(results, [])

    def test_stays_pending_when_still_not_done(self):
        sio.write_tracked("readline", {"last_action": "NEEDS_MINIMAL_PR",
                                         "last_checked": "2026-08-01T00:00:00+00:00"})
        with mock.patch("cf_core.reconciler.cfy.check_feedstock") as m:
            m.return_value = {"feedstock": "readline", "checked": True, "has_riscv64": False, "error": None}
            results = reconciler.reconcile_tracked(now_iso="2026-08-23T00:00:00+00:00")
        self.assertFalse(results[0]["changed"])
        updated = sio.read_tracked("readline")
        self.assertEqual(updated["last_action"], "NEEDS_MINIMAL_PR")
        self.assertEqual(updated["last_checked"], "2026-08-23T00:00:00+00:00")  # timestamp still refreshed

    def test_failed_check_does_not_crash_or_overwrite_action(self):
        sio.write_tracked("sqlite", {"last_action": "WAIT_NOT_STARTED",
                                       "last_checked": "2026-08-01T00:00:00+00:00"})
        with mock.patch("cf_core.reconciler.cfy.check_feedstock") as m:
            m.return_value = {"feedstock": "sqlite", "checked": False, "has_riscv64": None,
                               "error": "network"}
            results = reconciler.reconcile_tracked(now_iso="2026-08-23T00:00:00+00:00")
        self.assertFalse(results[0]["changed"])
        updated = sio.read_tracked("sqlite")
        self.assertEqual(updated["last_action"], "WAIT_NOT_STARTED")


class TestCheckReadyAlreadyDone(unittest.TestCase):
    def test_filters_to_confirmed_done_only(self):
        def fake_check(name):
            return {"feedstock": name, "checked": True, "has_riscv64": name == "mpg123", "error": None}

        with mock.patch("cf_core.reconciler.cfy.check_feedstock", side_effect=fake_check):
            result = reconciler.check_ready_already_done(["mpg123", "libffi", "isl"])
        self.assertEqual(result, ["mpg123"])

    def test_failed_checks_excluded_not_crashed_on(self):
        def fake_check(name):
            return {"feedstock": name, "checked": False, "has_riscv64": None, "error": "network"}

        with mock.patch("cf_core.reconciler.cfy.check_feedstock", side_effect=fake_check):
            result = reconciler.check_ready_already_done(["libffi"])
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
