"""Tests for cf_core.graph.

Run with: python3 -m unittest discover -s tests -v   (from the cf-bot repo root)

Uses stdlib unittest rather than pytest — no extra dependency needed to run these in whatever
environment cf-bot's cron actually executes in (pytest isn't guaranteed to be installed there).

Includes named regression tests for the three real bugs this session shipped and then had to
debug against real data:
  - test_cycle_does_not_crash_longest_chain   (graph_depth.py's original recursive DFS raised
    ValueError / would infinite-loop on a real cycle in the data)
  - test_dependencies_use_correct_direction   (an early networkx draft used nx.ancestors() instead
    of nx.descendants() and silently returned 0 results — wrong direction)
  - test_depth_histogram_not_reversed         (a leftover `.reverse()` call corrupted the
    shortest-depth histogram in one draft)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cf_core import graph as g


def diamond_feedstocks():
    """target depends on (a, b); both a and b depend on base. A simple diamond, no cycles.

        target
        /    \
       a      b
        \    /
         base
    """
    return {
        "target": {"immediate_children": []},
        "a": {"immediate_children": ["target"]},
        "b": {"immediate_children": ["target"]},
        "base": {"immediate_children": ["a", "b"]},
        "unrelated": {"immediate_children": []},
    }


def cyclic_feedstocks():
    """target -> a -> b -> c -> a (b/c/a form a real cycle), plus target -> d -> a (an
    alternate longer route into the same cycle so longest-hop-chain has something to pick)."""
    return {
        "target": {"immediate_children": []},
        "a": {"immediate_children": ["target", "d"]},
        "b": {"immediate_children": ["a"]},
        "c": {"immediate_children": ["b"]},
        "d": {"immediate_children": ["target"]},
        # close the cycle: c depends on a as well (a -> b -> c -> a)
    }


def cyclic_feedstocks_full():
    fs = cyclic_feedstocks()
    fs["c"]["immediate_children"] = ["b", "a"]  # a depends on c too -> a<-b<-c<-a cycle... see build
    return fs


class TestBuildGraph(unittest.TestCase):
    def test_edge_direction_matches_real_world_check(self):
        # Real sanity check from this session: libffi's immediate_children include python/cffi/
        # glib, all of which genuinely depend on libffi. So building the graph and asking for
        # libffi's *dependents* should NOT appear via parents_of/dependencies_of (those are for
        # libffi's own dependencies) -- python should depend ON libffi, i.e. libffi is a
        # dependency of python, i.e. libffi should appear in dependencies_of(python).
        fs = {
            "libffi": {"immediate_children": ["python", "cffi", "glib"]},
            "python": {"immediate_children": []},
            "cffi": {"immediate_children": []},
            "glib": {"immediate_children": []},
        }
        G = g.build_graph(fs)
        self.assertIn("libffi", g.dependencies_of(G, "python"))
        self.assertIn("libffi", g.parents_of(G, "python"))
        self.assertNotIn("python", g.dependencies_of(G, "libffi"))

    def test_isolated_and_unrelated_nodes_present_but_not_ancestors(self):
        G = g.build_graph(diamond_feedstocks())
        self.assertIn("unrelated", G.nodes)
        self.assertNotIn("unrelated", g.dependencies_of(G, "target"))


class TestDependenciesUseCorrectDirection(unittest.TestCase):
    """Regression test for the nx.ancestors() vs nx.descendants() bug."""

    def test_target_dependencies_are_its_own_prerequisites(self):
        G = g.build_graph(diamond_feedstocks())
        deps = g.dependencies_of(G, "target")
        self.assertEqual(deps, {"a", "b", "base"})
        # the wrong-direction bug returned an EMPTY set here (nx.ancestors on a fresh leaf-ward
        # target returns nodes that point TO target's dependents, not this)
        self.assertNotEqual(deps, set())

    def test_base_has_no_dependencies_of_its_own(self):
        G = g.build_graph(diamond_feedstocks())
        self.assertEqual(g.dependencies_of(G, "base"), set())


class TestDepthToTarget(unittest.TestCase):
    def test_diamond_depths(self):
        G = g.build_graph(diamond_feedstocks())
        depths = g.depth_to_target(G, "target")
        self.assertEqual(depths["target"], 0)
        self.assertEqual(depths["a"], 1)
        self.assertEqual(depths["b"], 1)
        self.assertEqual(depths["base"], 2)  # shortest path, even though base has 2 routes in
        self.assertNotIn("unrelated", depths)

    def test_depth_histogram_not_reversed(self):
        """Regression test for the leftover .reverse() bug that corrupted the histogram."""
        G = g.build_graph(diamond_feedstocks())
        depths = g.depth_to_target(G, "target")
        hist = g.depth_histogram(depths)
        # depth 0: just target (1 node); depth 1: a, b (2 nodes); depth 2: base (1 node)
        self.assertEqual(hist, {0: 1, 1: 2, 2: 1})
        # the bug produced a histogram keyed/ordered backwards relative to actual BFS distance;
        # explicitly assert depth 0 has fewer-or-equal nodes than would appear if reversed
        self.assertEqual(list(hist.keys()), sorted(hist.keys()))

    def test_missing_target_returns_empty(self):
        G = g.build_graph(diamond_feedstocks())
        self.assertEqual(g.depth_to_target(G, "does-not-exist"), {})


class TestCyclesAndLongestChain(unittest.TestCase):
    def _cyclic_graph(self):
        fs = {
            "target": {"immediate_children": ["d_child_of_target_but_not_used"]},
            "a": {"immediate_children": ["target"]},
            "b": {"immediate_children": ["a"]},
            "c": {"immediate_children": ["b", "a"]},  # a depends on b and c; c depends on b and a
        }
        # Build an explicit real cycle among a/b/c: a -> b -> c -> a
        fs = {
            "target": {"immediate_children": []},
            "a": {"immediate_children": ["target"]},
            "b": {"immediate_children": ["a"]},
            "c": {"immediate_children": ["b"]},
        }
        # close the loop: make "a" also a child of "c" (a depends on c) -> a->c->b->a cycle
        fs["a"]["immediate_children"] = ["target", "c"]
        return fs

    def test_cycle_does_not_crash_longest_chain(self):
        """Regression test for the original recursive-DFS implementation that raised/crashed on
        a real cycle. This must complete without raising and return a well-defined finite result."""
        fs = self._cyclic_graph()
        G = g.build_graph(fs)
        try:
            length, chain = g.longest_hop_chain(G, "target")
        except RecursionError:
            self.fail("longest_hop_chain crashed on a cyclic graph (the original bug)")
        self.assertIsInstance(length, int)
        self.assertGreaterEqual(length, 1)
        self.assertTrue(chain)

    def test_cycle_detected_with_full_membership(self):
        fs = self._cyclic_graph()
        G = g.build_graph(fs)
        deps = g.dependencies_of(G, "target")
        cycles = g.find_cycles(G, within=deps | {"target"})
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0], sorted(["a", "b", "c"]))

    def test_no_cycles_in_acyclic_graph(self):
        G = g.build_graph(diamond_feedstocks())
        self.assertEqual(g.find_cycles(G), [])

    def test_longest_chain_acyclic_diamond(self):
        G = g.build_graph(diamond_feedstocks())
        length, chain = g.longest_hop_chain(G, "target")
        self.assertEqual(length, 2)
        self.assertEqual(chain, ["target", "a", "base"] if chain[1] == "a" else ["target", "b", "base"])


class TestSummarize(unittest.TestCase):
    def test_summarize_shape(self):
        result = g.summarize(diamond_feedstocks(), "target")
        self.assertTrue(result["found"])
        self.assertEqual(result["num_dependencies"], 3)
        self.assertEqual(set(result["direct_dependencies"]), {"a", "b"})
        self.assertEqual(result["longest_hop_chain_length"], 2)

    def test_summarize_missing_target(self):
        result = g.summarize(diamond_feedstocks(), "nope")
        self.assertFalse(result["found"])


class TestAgainstRealData(unittest.TestCase):
    """Golden-fixture check against a frozen real snapshot of the migration JSON, if present.
    Skipped automatically if the fixture hasn't been placed (e.g. in an offline CI run) --
    fetch it once with the git-clone technique in cf_core/migration_source.py and drop it at
    tests/fixtures/migration_snapshot.json to enable this test locally."""

    FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "migration_snapshot.json")

    def test_real_data_sanity(self):
        if not os.path.exists(self.FIXTURE):
            self.skipTest("no frozen fixture at tests/fixtures/migration_snapshot.json")
        import json

        with open(self.FIXTURE) as f:
            data = json.load(f)
        feedstocks = data["_feedstock_status"]
        G = g.build_graph(feedstocks)
        self.assertIn("conda-forge-ci-setup", G.nodes)
        self.assertIn("python", g.dependencies_of(G, "conda-forge-ci-setup"))
        self.assertIn("libffi", g.dependencies_of(G, "python"))
        # tk was found NOT to be a real dependency of python this session (it appeared in a PR
        # comment mention but not in the actual graph) -- lock that in as a regression check.
        self.assertNotIn("tk", g.dependencies_of(G, "python"))


if __name__ == "__main__":
    unittest.main()
