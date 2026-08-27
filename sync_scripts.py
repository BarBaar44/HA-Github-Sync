#!/usr/bin/env python3
"""
sync_scripts.py

Bidirectional sync between Home Assistant's single scripts.yaml (what
the UI editor reads/writes) and scripts/ (one file per script, kept
for clean git history on GitHub). Mirrors sync_automations.py's logic,
adapted for scripts.yaml's structure: a dict keyed by script id
(object_id), not a list. The dict key itself is always the unique
identifier -- there's no separate id/alias field to validate, unlike
automations.

Unlike automations/, scripts/ is NOT meant to persist permanently on
local disk -- see git_sync.sh, which materializes it only for the
duration of each sync run (restored from the last commit at the
start, deleted again at the end, EXCEPT after a conflict or
validation failure -- see git_sync.sh's cleanup logic) so that
scripts.yaml remains the only thing visible in /config between runs,
and stays fully UI-editable. This script itself doesn't know or care
about that -- it just reads/writes whatever's on disk at scripts/
when it runs.

Uses PyYAML (ships with Home Assistant Core itself) for the same
reason as sync_automations.py: guaranteed available wherever
shell_command executes, no separate install. Comments are not
preserved across a sync.

How it decides direction (normal mode, no --force):
- Hashes the current content of both sides.
- Compares against the hashes recorded at the last successful sync
  (stored in .scripts_sync_state.yaml).
- Whichever side's hash changed gets propagated to the other side.
- If BOTH changed since the last sync, that's a real conflict --
  nothing is touched, exits 2.

Before touching anything (normal or forced), validates that no script
id is defined in more than one split file (that CAN happen across
files even though a single YAML mapping can't have a literal
duplicate key within itself). If found, nothing is written -- exits 3.

Usage:
    python3 sync_scripts.py                # normal two-way sync
    python3 sync_scripts.py --bootstrap    # ONE-TIME: split the
                                            # current scripts.yaml
                                            # into scripts/ and
                                            # record the baseline.
                                            # Refuses to run if
                                            # scripts/ already has
                                            # files in it.
    python3 sync_scripts.py --force flat   # conflict resolution:
                                            # skip the conflict check
                                            # entirely, unconditionally
                                            # regenerate scripts/ from
                                            # the current scripts.yaml
                                            # ("local wins"). Also
                                            # works with no prior
                                            # baseline -- doubles as
                                            # an alternative to
                                            # --bootstrap if the state
                                            # file is ever lost.
    python3 sync_scripts.py --force split  # conflict resolution:
                                            # unconditionally
                                            # regenerate scripts.yaml
                                            # from the current
                                            # scripts/ ("git wins").

Exit codes:
    0 - nothing to do, or synced cleanly (or forced flat->split), no
        reload needed
    1 - synced (or forced) split -> scripts.yaml direction; HA needs
        script.reload to pick it up
    2 - conflict detected, nothing changed, needs manual resolution
        (re-run with --force flat or --force split)
    3 - validation failed (a script id is defined in more than one
        split file), no baseline recorded yet (normal mode only,
        run --bootstrap or --force first), or a bad argument --
        nothing changed, needs manual resolution
"""
import hashlib
import re
import sys
from pathlib import Path

import yaml

CONFIG_DIR = Path("/config")
SCRIPTS_FILE = CONFIG_DIR / "scripts.yaml"
SPLIT_DIR = CONFIG_DIR / "scripts"
STATE_FILE = CONFIG_DIR / ".scripts_sync_state.yaml"


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "script"


def dump_to_str(data) -> str:
    return yaml.dump(
        data, default_flow_style=False, sort_keys=False, allow_unicode=True, width=4096
    )


def load_flat() -> dict:
    if not SCRIPTS_FILE.exists():
        return {}
    return yaml.safe_load(SCRIPTS_FILE.read_text()) or {}


def load_split():
    """Returns (combined_dict, problems). A duplicate key across
    different split files is a problem; PyYAML already silently
    collapses a duplicate key WITHIN a single file before we ever see
    it, so that case can't be detected here."""
    combined = {}
    problems = []
    seen_in_file = {}
    if not SPLIT_DIR.exists():
        return combined, problems
    for f in sorted(SPLIT_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
        for key, value in data.items():
            if key in seen_in_file:
                problems.append(
                    f"scripts/: id '{key}' is defined in both "
                    f"'{seen_in_file[key]}' and '{f.name}'."
                )
            seen_in_file[key] = f.name
            combined[key] = value
    return combined, problems


def canonical_hash(scripts: dict) -> str:
    ordered = {k: scripts[k] for k in sorted(scripts.keys(), key=str)}
    return hashlib.sha256(dump_to_str(ordered).encode()).hexdigest()


def write_flat(scripts: dict):
    ordered = {k: scripts[k] for k in sorted(scripts.keys(), key=str)}
    SCRIPTS_FILE.write_text(dump_to_str(ordered))


def write_split(scripts: dict):
    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear existing split files -- flat side is authoritative for this direction.
    for f in SPLIT_DIR.glob("*.yaml"):
        f.unlink()

    for key, value in scripts.items():
        filename = slugify(str(key))
        out_file = SPLIT_DIR / f"{filename}.yaml"
        out_file.write_text(dump_to_str({key: value}))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return yaml.safe_load(STATE_FILE.read_text()) or {}


def save_state(flat_hash: str, split_hash: str):
    STATE_FILE.write_text(
        dump_to_str({"last_flat_hash": flat_hash, "last_split_hash": split_hash})
    )


def bootstrap():
    """One-time: split the current scripts.yaml into scripts/ and
    record the baseline state. Refuses to run if scripts/ already has
    files in it, to avoid clobbering an existing split by accident.
    (If you need to force-overwrite an existing split, use
    --force flat instead -- it has no such guard rail.)"""
    existing_split_files = list(SPLIT_DIR.glob("*.yaml")) if SPLIT_DIR.exists() else []
    if existing_split_files:
        print(
            f"scripts/ already contains {len(existing_split_files)} file(s) -- "
            "refusing to bootstrap over existing data. Delete/move them first, "
            "or use --force flat if you deliberately want to overwrite them.",
            file=sys.stderr,
        )
        sys.exit(3)

    flat_scripts = load_flat()
    if not flat_scripts:
        print("scripts.yaml is empty or missing -- nothing to bootstrap.")
        sys.exit(0)

    write_split(flat_scripts)
    flat_hash = canonical_hash(flat_scripts)
    split_scripts, _ = load_split()
    split_hash = canonical_hash(split_scripts)
    save_state(flat_hash, split_hash)
    print(f"Bootstrapped: split {len(flat_scripts)} script(s) into {SPLIT_DIR}/")
    sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--bootstrap":
        bootstrap()
        return

    force = None
    if len(sys.argv) > 1:
        if sys.argv[1] == "--force" and len(sys.argv) > 2 and sys.argv[2] in ("flat", "split"):
            force = sys.argv[2]
        else:
            print(f"Unrecognized arguments: {sys.argv[1:]}", file=sys.stderr)
            print("Usage: sync_scripts.py [--bootstrap | --force flat|split]", file=sys.stderr)
            sys.exit(3)

    flat_scripts = load_flat()
    split_scripts, split_problems = load_split()

    if split_problems:
        print("VALIDATION FAILED -- nothing changed:", file=sys.stderr)
        for p in split_problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(3)

    current_flat_hash = canonical_hash(flat_scripts)
    current_split_hash = canonical_hash(split_scripts)

    if force == "flat":
        write_split(flat_scripts)
        new_split_hash = canonical_hash(load_split()[0])
        save_state(current_flat_hash, new_split_hash)
        print(
            f"FORCED scripts.yaml -> scripts/ ({len(flat_scripts)} scripts). "
            "scripts/ edits discarded."
        )
        sys.exit(0)

    if force == "split":
        write_flat(split_scripts)
        new_flat_hash = canonical_hash(load_flat())
        save_state(new_flat_hash, current_split_hash)
        print(
            f"FORCED scripts/ -> scripts.yaml ({len(split_scripts)} scripts). "
            "scripts.yaml edits discarded."
        )
        sys.exit(1)

    state = load_state()
    last_flat_hash = state.get("last_flat_hash")
    last_split_hash = state.get("last_split_hash")

    if last_flat_hash is None and last_split_hash is None:
        print(
            "No baseline recorded yet. Run 'python3 sync_scripts.py --bootstrap' "
            "once first.",
            file=sys.stderr,
        )
        sys.exit(3)

    flat_changed = current_flat_hash != last_flat_hash
    split_changed = current_split_hash != last_split_hash

    if not flat_changed and not split_changed:
        print("No changes on either side.")
        sys.exit(0)

    if flat_changed and split_changed:
        print(
            "CONFLICT: both scripts.yaml and scripts/ changed since the last "
            "sync. Not touching either file -- run with '--force flat' to "
            "keep scripts.yaml, or '--force split' to keep scripts/, then "
            "this will sync cleanly.",
            file=sys.stderr,
        )
        sys.exit(2)

    if flat_changed:
        write_split(flat_scripts)
        new_split_hash = canonical_hash(load_split()[0])
        save_state(current_flat_hash, new_split_hash)
        print(f"Synced scripts.yaml -> scripts/ ({len(flat_scripts)} scripts).")
        sys.exit(0)

    if split_changed:
        write_flat(split_scripts)
        new_flat_hash = canonical_hash(load_flat())
        save_state(new_flat_hash, current_split_hash)
        print(f"Synced scripts/ -> scripts.yaml ({len(split_scripts)} scripts).")
        sys.exit(1)


if __name__ == "__main__":
    main()
