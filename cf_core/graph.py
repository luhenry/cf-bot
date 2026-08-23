"""
cf_core.graph — the single implementation of the riscv64-migration dependency graph.

Supersedes two implementations that diverged and bugged out independently this session:
  - riscv64_status.py's hand-rolled `build_parents_map()` (dict of lists) + `compute_depths_to_target()`
    (hand-rolled BFS)
  - graph_depth.py's hand-rolled recursive DFS longest-path (crashed on a real cycle in the data —
    the DAG-longest-path assumption was wrong, the graph has real cycles) and its later
    networkx-based replacement graph_depth2.py (which itself first shipped with an
    nx.ancestors()-vs-nx.descendants() direction bug, and a leftover `.reverse()` call that
    corrupted the shortest-depth histogram)

Edge convention (load-bearing, verified against real data — do not change without re-verifying):
migration JSON's `immediate_children[Y]` lists nodes that DEPEND ON Y (Y's dependents; e.g.
libffi's immediate_children includes python/cffi/glib, all of which really do depend on libffi).
We store the edge as `child -> parent` (i.e. "child depends on parent"). Under this convention:
  - `G.successors(X)`      = X's own direct dependencies (its prerequisites)
  - `nx.descendants(G, X)` = X's own *transitive* dependencies (walking forward along
                             child->parent edges from X reaches things X depends on)
  - `nx.single_source_shortest_path_length(G, X)` = shortest hop-count from X to each of its
                             transitive dependencies (BFS along the same child->parent edges)
Using `nx.ancestors(G, X)` here would be backwards — it returns nodes that have a path *to* X,
i.e. X's dependents, not its dependencies. This is exactly the bug that shipped once already.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

import networkx as nx


def build_graph(feedstocks: dict) -> nx.DiGraph:
    """Build the dependency graph from migration JSON's `_feedstock_status` mapping.

    One edge per (child, parent) pair where child depends on parent, per the module docstring's
    edge convention. Nodes with no listed children still appear (as isolated or leaf nodes).
    """
    G = nx.DiGraph()
    G.add_nodes_from(feedstocks.keys())
    for name, info in feedstocks.items():
        for child in info.get("immediate_children", []) or []:
            if child in feedstocks:
                G.add_edge(child, name)
    return G


def parents_of(G: nx.DiGraph, node: str) -> list[str]:
    """Direct dependencies (prerequisites) of `node` — sorted for determinism."""
    if node not in G:
        return []
    return sorted(G.successors(node))


def dependencies_of(G: nx.DiGraph, node: str) -> set[str]:
    """All transitive dependencies of `node` (every node it depends on, directly or indirectly)."""
    if node not in G:
        return set()
    return nx.descendants(G, node)


def depth_to_target(G: nx.DiGraph, target: str) -> dict[str, int]:
    """Shortest hop-count from `target` to each of its transitive dependencies.

    depths[target] == 0; a direct dependency is 1; etc. Nodes not returned are not ancestors
    (in the dependency sense) of `target` at all — they're either downstream of it or in an
    unrelated part of the graph. BFS is cycle-safe by construction, so this is correct even
    though the real graph has genuine cycles (see find_cycles below).
    """
    if target not in G:
        return {}
    return dict(nx.single_source_shortest_path_length(G, target))


def depth_histogram(depths: dict[str, int]) -> dict[int, int]:
    """depth -> count of nodes at that depth, sorted by depth ascending."""
    return dict(sorted(Counter(depths.values()).items()))


def find_cycles(G: nx.DiGraph, within: Optional[Iterable[str]] = None) -> list[list[str]]:
    """Non-trivial strongly-connected components (real cycles), each a sorted member list,
    sorted by (size desc, first member) for determinism. Restrict the search to `within`
    (e.g. a dependency-ancestor set) if given — irrelevant parts of a large graph don't need
    to be scanned.
    """
    sub = G.subgraph(within) if within is not None else G
    sccs = [sorted(s) for s in nx.strongly_connected_components(sub) if len(s) > 1]
    sccs.sort(key=lambda s: (-len(s), s[0]))
    return sccs


def longest_hop_chain(
    G: nx.DiGraph, source: str, within: Optional[Iterable[str]] = None
) -> tuple[int, list[str]]:
    """Longest simple hop-chain starting at `source`, walking into its dependencies.

    Cycles are collapsed via strongly-connected-component condensation first, so "longest path"
    is always well-defined even though the real graph has genuine cycles (a naive recursive DFS
    over the raw graph will crash/loop forever on a cycle — this is exactly the bug that shipped
    in an earlier draft of this logic). Returns (length_in_hops, chain) where each chain entry is
    either a bare node name or "[a <-> b <-> ...]" for a collapsed multi-node cycle.
    """
    if source not in G:
        return 0, []

    ancestor_nodes = nx.descendants(G, source) | {source}
    if within is not None:
        ancestor_nodes &= set(within) | {source}

    sub = G.subgraph(ancestor_nodes)
    C = nx.condensation(sub)
    mapping = C.graph["mapping"]
    source_scc = mapping[source]

    reachable = nx.descendants(C, source_scc) | {source_scc}
    sub_c = C.subgraph(reachable)
    topo = list(nx.topological_sort(sub_c))

    best_len = {n: 0 for n in sub_c.nodes}
    best_next: dict[int, Optional[int]] = {n: None for n in sub_c.nodes}
    for n in reversed(topo):
        for succ in sub_c.successors(n):
            if best_len[succ] + 1 > best_len[n]:
                best_len[n] = best_len[succ] + 1
                best_next[n] = succ

    length = best_len[source_scc]
    chain = [source_scc]
    cur = source_scc
    while best_next[cur] is not None:
        cur = best_next[cur]
        chain.append(cur)

    def label(scc_id: int) -> str:
        members = sorted(C.nodes[scc_id]["members"])
        return members[0] if len(members) == 1 else "[" + " <-> ".join(members) + "]"

    return length, [label(n) for n in chain]


def summarize(feedstocks: dict, target: str) -> dict:
    """One-shot convenience: everything riscv64_status.py --depth and graph_depth.py both
    wanted, from a single graph build. Used by the `cf_core graph` CLI verb."""
    G = build_graph(feedstocks)
    if target not in G:
        return {"target": target, "found": False}

    deps = dependencies_of(G, target)
    depths = depth_to_target(G, target)
    cycles = find_cycles(G, within=deps | {target})
    length, chain = longest_hop_chain(G, target, within=deps | {target})

    return {
        "target": target,
        "found": True,
        "total_feedstocks": G.number_of_nodes(),
        "num_dependencies": len(deps),
        "direct_dependencies": parents_of(G, target),
        "depths": depths,
        "depth_histogram": depth_histogram(depths),
        "cycles": cycles,
        "longest_hop_chain_length": length,
        "longest_hop_chain": chain,
    }
