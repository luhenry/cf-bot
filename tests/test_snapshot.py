import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import snapshot as snap


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "_snapshot")

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_previous_snapshot(self):
        data = {"_feedstock_status": {}, "done": ["a"], "in-pr": ["b"]}
        diff = snap.diff_against_last(data, snapshot_dir=self.dir)
        self.assertFalse(diff["has_previous"])

    def test_diff_detects_newly_done_and_dropped(self):
        first = {
            "_feedstock_status": {"a": {}, "b": {}, "c": {}},
            "done": ["a"],
            "in-pr": ["b", "c"],
        }
        snap.save_snapshot(first, now_iso="2026-08-21T07:30:00+00:00", snapshot_dir=self.dir)

        # mpg123-style case: "c" merged (dropped from in-pr, not showing up as newly done in
        # THIS fetch either, e.g. because it rolled fully off tracking) -- and "b" became done.
        second = {
            "_feedstock_status": {"a": {}, "b": {}, "d": {}},
            "done": ["a", "b"],
            "in-pr": ["d"],
        }
        diff = snap.diff_against_last(second, snapshot_dir=self.dir)

        self.assertTrue(diff["has_previous"])
        self.assertEqual(diff["newly_done"], ["b"])
        self.assertEqual(diff["newly_in_pr"], ["d"])
        self.assertEqual(diff["dropped_from_in_pr"], ["c"])

    def test_save_then_load_latest(self):
        data = {"_feedstock_status": {"x": {}}, "done": [], "in-pr": ["x"]}
        snap.save_snapshot(data, now_iso="2026-08-23T00:00:00+00:00", snapshot_dir=self.dir)
        latest = snap.load_latest(snapshot_dir=self.dir)
        self.assertEqual(latest["in_pr"], ["x"])
        self.assertEqual(latest["total_feedstocks"], 1)

    def test_timestamped_file_also_written(self):
        data = {"_feedstock_status": {}, "done": [], "in-pr": []}
        snap.save_snapshot(data, now_iso="2026-08-23T00:00:00+00:00", snapshot_dir=self.dir)
        files = os.listdir(self.dir)
        self.assertIn("latest.json", files)
        self.assertTrue(any(f != "latest.json" for f in files))


if __name__ == "__main__":
    unittest.main()
