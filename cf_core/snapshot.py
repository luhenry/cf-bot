"""
cf_core.snapshot — persist the fetched migration graph across runs and diff against the
previous run.

Before this module existed, "what changed since last run" had no first-class answer -- this
session inferred that mpg123 had probably merged only by comparing an old report against a fresh
fetch by hand. Purely additive: writes to state/_snapshot/, a new directory that never touches
existing state/*.json or state/_tracked/*.json files.
"""
from __future__ import annotations

import json
import os
from typing import Optional

SNAPSHOT_DIR = os.path.join("state", "_snapshot")
LATEST_PATH = os.path.join(SNAPSHOT_DIR, "latest.json")


def _timestamped_path(now_iso: str) -> str:
    safe = now_iso.replace(":", "").replace(" ", "T")
    return os.path.join(SNAPSHOT_DIR, f"{safe}.json")


def save_snapshot(migration_data: dict, now_iso: str, snapshot_dir: str = SNAPSHOT_DIR) -> dict:
    os.makedirs(snapshot_dir, exist_ok=True)
    summary = {
        "fetched_at": now_iso,
        "total_feedstocks": len(migration_data.get("_feedstock_status", {})),
        "done": sorted(migration_data.get("done", [])),
        "in_pr": sorted(migration_data.get("in-pr", [])),
    }
    safe = now_iso.replace(":", "").replace(" ", "T")
    with open(os.path.join(snapshot_dir, f"{safe}.json"), "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    with open(os.path.join(snapshot_dir, "latest.json"), "w") as f:
        json.dump(summary, f, indent=2)
        f.write("\n")
    return summary


def load_latest(snapshot_dir: str = SNAPSHOT_DIR) -> Optional[dict]:
    path = os.path.join(snapshot_dir, "latest.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def diff_against_last(migration_data: dict, snapshot_dir: str = SNAPSHOT_DIR) -> dict:
    """Compare the just-fetched migration data's done/in-pr sets against the last saved
    snapshot (if any). Call save_snapshot() separately, typically right after, so the diff
    compares against the PREVIOUS run rather than itself."""
    previous = load_latest(snapshot_dir)
    if previous is None:
        return {"has_previous": False, "newly_done": [], "newly_in_pr": [], "dropped_from_in_pr": []}

    prev_done = set(previous.get("done", []))
    prev_in_pr = set(previous.get("in_pr", []))
    cur_done = set(migration_data.get("done", []))
    cur_in_pr = set(migration_data.get("in-pr", []))

    return {
        "has_previous": True,
        "previous_fetched_at": previous.get("fetched_at"),
        "newly_done": sorted(cur_done - prev_done),
        "newly_in_pr": sorted(cur_in_pr - prev_in_pr),
        # Dropped from in-pr without becoming "done" in this same fetch: most likely merged and
        # already rolled off tracking (this is exactly the mpg123 case from this session,
        # inferred by hand before this module existed), but could also be a closed-without-
        # merging PR -- worth a human glance either way, hence surfaced rather than silently
        # dropped.
        "dropped_from_in_pr": sorted((prev_in_pr - cur_in_pr) - cur_done),
    }
