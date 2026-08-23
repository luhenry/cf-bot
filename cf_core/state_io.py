"""
cf_core.state_io — deterministic, validated read/merge/write for state/*.json and
state/_tracked/*.json.

Before this module existed, both workflow files wrote these files via an LLM agent's "Write tool
directly" step -- a mechanical read-merge-write operation with zero judgment content was on the
LLM critical path, at real risk of dropping or mangling a field. This module makes that
deterministic: it validates `last_action` against the policy-defined action-code enum before
writing (refusing to write an unrecognized value), and merges additively so a field present in an
existing file but omitted from a new write is preserved, never silently dropped.

Field names, types, and file locations are UNCHANGED from before this rearchitecture -- this
module documents and enforces the existing implicit schema, it does not redesign it. Any new
field (e.g. `schema_version`, or a namespaced object like `upstream_tracking`) is additive only.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Optional

from cf_core import policy

STATE_DIR = "state"
TRACKED_DIR = os.path.join(STATE_DIR, "_tracked")


def state_path(feedstock: str, base_dir: str = STATE_DIR) -> str:
    return os.path.join(base_dir, f"{feedstock}.json")


def tracked_path(name: str, base_dir: str = TRACKED_DIR) -> str:
    return os.path.join(base_dir, f"{name}.json")


def _read_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _atomic_write_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class InvalidActionCode(ValueError):
    """Raised when a write's `last_action` isn't in the policy-defined enum for that file kind."""


def read_state(feedstock: str, base_dir: str = STATE_DIR) -> Optional[dict]:
    return _read_json(state_path(feedstock, base_dir))


def read_tracked(name: str, base_dir: str = TRACKED_DIR) -> Optional[dict]:
    return _read_json(tracked_path(name, base_dir))


def write_state(feedstock: str, fields: dict, base_dir: str = STATE_DIR) -> dict:
    """Additive merge onto any existing state/<feedstock>.json, validate last_action, atomic
    write. Returns the final merged dict that was written. Existing fields not present in
    `fields` are preserved untouched (this is the forward-compatibility guarantee in code)."""
    action = fields.get("last_action")
    if action is not None and action not in policy.ACTION_CODES:
        raise InvalidActionCode(
            f"{action!r} is not a recognized action code (see cf_core.policy.ACTION_CODES)"
        )
    existing = read_state(feedstock, base_dir) or {}
    merged = {**existing, **fields, "feedstock": feedstock}
    _atomic_write_json(state_path(feedstock, base_dir), merged)
    return merged


def write_tracked(name: str, fields: dict, base_dir: str = TRACKED_DIR) -> dict:
    action = fields.get("last_action")
    if action is not None and action not in policy.TRACKED_ACTION_CODES:
        raise InvalidActionCode(
            f"{action!r} is not a recognized tracked action code (see cf_core.policy.TRACKED_ACTION_CODES)"
        )
    existing = read_tracked(name, base_dir) or {}
    merged = {**existing, **fields, "name": name}
    _atomic_write_json(tracked_path(name, base_dir), merged)
    return merged


def list_state_names(base_dir: str = STATE_DIR) -> list[str]:
    if not os.path.isdir(base_dir):
        return []
    return sorted(
        fn[:-5] for fn in os.listdir(base_dir)
        if fn.endswith(".json") and os.path.isfile(os.path.join(base_dir, fn))
    )


def list_tracked_names(base_dir: str = TRACKED_DIR) -> list[str]:
    if not os.path.isdir(base_dir):
        return []
    return sorted(fn[:-5] for fn in os.listdir(base_dir) if fn.endswith(".json"))
