"""cf_core — the single deterministic-logic package for cf-bot.

Owns everything that has no judgment content: graph traversal, policy predicates, the
authoritative conda-forge.yml check, state file I/O, shadow-dependency reconciliation, and
migration-graph snapshots. See CLAUDE.md for how the JS workflows call into this package
(via `python3 -m cf_core <verb>` relay calls) and where LLM judgment is still used.
"""
