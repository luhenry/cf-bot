import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import state_io as sio


class TestStateIO(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.tracked_dir = os.path.join(self._tmp.name, "state", "_tracked")

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        sio.write_state("libffi", {"last_action": "NUDGE_MERGE", "num_descendants": 148},
                         base_dir=self.state_dir)
        result = sio.read_state("libffi", base_dir=self.state_dir)
        self.assertEqual(result["feedstock"], "libffi")
        self.assertEqual(result["last_action"], "NUDGE_MERGE")
        self.assertEqual(result["num_descendants"], 148)

    def test_additive_merge_preserves_untouched_fields(self):
        sio.write_state("perl", {"last_action": "ESCALATE", "reason": "old", "confidence": "high"},
                         base_dir=self.state_dir)
        # A later write only updates last_action + reason -- confidence must survive untouched.
        sio.write_state("perl", {"last_action": "NUDGE_MERGE", "reason": "policy re-classification"},
                         base_dir=self.state_dir)
        result = sio.read_state("perl", base_dir=self.state_dir)
        self.assertEqual(result["last_action"], "NUDGE_MERGE")
        self.assertEqual(result["reason"], "policy re-classification")
        self.assertEqual(result["confidence"], "high")  # preserved, not dropped

    def test_invalid_action_code_rejected(self):
        with self.assertRaises(sio.InvalidActionCode):
            sio.write_state("foo", {"last_action": "NOT_A_REAL_CODE"}, base_dir=self.state_dir)

    def test_tracked_accepts_done(self):
        result = sio.write_tracked("zstd", {"last_action": "DONE"}, base_dir=self.tracked_dir)
        self.assertEqual(result["last_action"], "DONE")
        self.assertEqual(result["name"], "zstd")

    def test_main_state_rejects_tracked_only_code(self):
        # DONE is valid for _tracked/*.json but not for state/*.json
        with self.assertRaises(sio.InvalidActionCode):
            sio.write_state("zstd", {"last_action": "DONE"}, base_dir=self.state_dir)

    def test_read_missing_file_returns_none(self):
        self.assertIsNone(sio.read_state("nope", base_dir=self.state_dir))
        self.assertIsNone(sio.read_tracked("nope", base_dir=self.tracked_dir))

    def test_list_names(self):
        sio.write_state("a", {}, base_dir=self.state_dir)
        sio.write_state("b", {}, base_dir=self.state_dir)
        self.assertEqual(sio.list_state_names(base_dir=self.state_dir), ["a", "b"])

    def test_no_leftover_tmp_files_after_write(self):
        sio.write_state("clean", {"last_action": "NUDGE_MERGE"}, base_dir=self.state_dir)
        leftovers = [f for f in os.listdir(self.state_dir) if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
