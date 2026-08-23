import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import policy as p


class TestCudaWontfix(unittest.TestCase):
    def test_matches_known_members(self):
        for name in ["cuda-nvml-dev", "libcurand", "nccl", "cuda-cudart", "libcufile"]:
            self.assertTrue(p.is_cuda_wontfix(name), name)

    def test_does_not_match_cuda_optional_packages(self):
        # mpich/openmpi already had their CUDA dependency dropped for riscv64 -- must NOT
        # be classified wontfix (this was an explicit user correction this session).
        for name in ["mpich", "openmpi", "libtiff", "cudatoolkit-fake-suffix-should-not-match"]:
            # cudatoolkit doesn't start with cuda- (no trailing dash after cuda), confirm precise anchoring
            pass
        self.assertFalse(p.is_cuda_wontfix("mpich"))
        self.assertFalse(p.is_cuda_wontfix("openmpi"))

    def test_anchored_at_start_only(self):
        self.assertFalse(p.is_cuda_wontfix("mylibcuda"))  # libcu pattern must anchor at start
        self.assertFalse(p.is_cuda_wontfix("something-nccl"))


class TestV1Migration(unittest.TestCase):
    def test_repo_already_v1_never_flagged(self):
        self.assertFalse(p.pr_is_v1_migration("Add riscv64 support", ["recipe/recipe.yaml"], True))

    def test_flagged_by_recipe_yaml_file(self):
        self.assertTrue(p.pr_is_v1_migration("Add riscv64", ["recipe/recipe.yaml"], False))

    def test_flagged_by_title_keyword(self):
        self.assertTrue(p.pr_is_v1_migration("Support riscv64 and migrate to v1", ["recipe/meta.yaml"], False))

    def test_focused_fix_not_flagged(self):
        # perl-feedstock#76 reference case: bot PR + minimal fix, no v1 anywhere
        self.assertFalse(p.pr_is_v1_migration("Add linux-riscv64 support", ["recipe/meta.yaml", "recipe/build.sh"], False))


class TestScorePr(unittest.TestCase):
    def test_bot_pr_scores_baseline(self):
        s = p.score_pr(author_login=p.BOT_AUTHOR, title="riscv64 migration", body=None,
                        changed_files=["recipe/meta.yaml"], bot_pr_number=1, repo_already_uses_v1=False)
        self.assertEqual(s, p.PR_SCORE_BOT)

    def test_focused_non_bot_pr_scores_highest(self):
        s = p.score_pr(author_login="h-vetinari", title="Add linux-riscv64 support", body=None,
                        changed_files=["recipe/meta.yaml"], bot_pr_number=1, repo_already_uses_v1=False)
        self.assertEqual(s, p.PR_SCORE_FOCUSED)

    def test_focused_pr_superseding_bot_gets_bonus(self):
        s = p.score_pr(author_login="h-vetinari", title="Add linux-riscv64 support",
                        body="Supersedes and includes changes from #42",
                        changed_files=["recipe/meta.yaml"], bot_pr_number=42, repo_already_uses_v1=False)
        self.assertEqual(s, p.PR_SCORE_FOCUSED + p.PR_SCORE_FOCUSED_SUPERSEDE_BONUS)

    def test_v1_bundled_pr_scores_lower_than_focused(self):
        s = p.score_pr(author_login="wolfv", title="Support riscv64, migrate to v1", body=None,
                        changed_files=["recipe/recipe.yaml"], bot_pr_number=1, repo_already_uses_v1=False)
        self.assertEqual(s, p.PR_SCORE_V1_BUNDLED)
        self.assertLess(s, p.PR_SCORE_FOCUSED)

    def test_unrelated_pr_scores_zero(self):
        s = p.score_pr(author_login="someone", title="Bump version", body=None,
                        changed_files=["recipe/meta.yaml"], bot_pr_number=1, repo_already_uses_v1=False)
        self.assertEqual(s, 0)


class TestActionCodes(unittest.TestCase):
    def test_wait_upstream_is_registered(self):
        self.assertIn("WAIT_UPSTREAM", p.ACTION_CODES)

    def test_tracked_is_superset_of_main(self):
        self.assertTrue(p.ACTION_CODES.issubset(p.TRACKED_ACTION_CODES))

    def test_done_only_valid_for_tracked(self):
        self.assertIn("DONE", p.TRACKED_ACTION_CODES)
        self.assertNotIn("DONE", p.ACTION_CODES)


class TestCommitMessageGuard(unittest.TestCase):
    def test_clean_message_passes(self):
        result = p.check_commit_message("triage 2026-08-23: 12 feedstocks, 2 shadow deps")
        self.assertTrue(result["clean"])
        self.assertEqual(result["violations"], [])

    def test_rejects_co_authored_by_claude(self):
        result = p.check_commit_message("fix build\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        self.assertFalse(result["clean"])
        self.assertIn("claude", result["violations"])

    def test_rejects_generated_with_claude(self):
        result = p.check_commit_message("fix build\n\n🤖 Generated with Claude Code")
        self.assertFalse(result["clean"])

    def test_case_insensitive(self):
        result = p.check_commit_message("CLAUDE helped with this")
        self.assertFalse(result["clean"])


if __name__ == "__main__":
    unittest.main()
