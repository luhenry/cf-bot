import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import conda_forge_yml_check as cfy

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


class TestCheckFeedstockLive(unittest.TestCase):
    """Hits the real network via the git-clone fallback -- same live checks performed by hand
    this session for zstd (confirmed done) and pandoc (confirmed not yet done). Skipped if
    network is unavailable in this environment."""

    def test_zstd_confirmed_done(self):
        result = cfy.check_feedstock("zstd")
        if not result["checked"]:
            self.skipTest("network unavailable for live check")
        self.assertTrue(result["has_riscv64"])

    def test_pandoc_not_yet_done(self):
        result = cfy.check_feedstock("pandoc")
        if not result["checked"]:
            self.skipTest("network unavailable for live check")
        self.assertFalse(result["has_riscv64"])


if __name__ == "__main__":
    unittest.main()
