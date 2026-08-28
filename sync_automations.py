#!/usr/bin/env python3
"""
sync_automations.py

Bidirectional sync between Home Assistant's single automations.yaml
(what the UI editor reads/writes) and automations/ (one file per
automation, kept for clean git history on GitHub).

automations/ is NOT meant to persist permanently on local disk -- see
git_sync.sh, which materializes it only for the duration of each sync
run (restored from the last commit at the start, deleted again at the
end -- except after a conflict or validation failure, when it's left
in place so there's something to inspect) so that automations.yaml
remains the only thing visible in /config between runs, and stays
fully UI-editable. This script itself doesn't know or care about
that -- it just reads/writes whatever's on disk at automations/ when
it runs. This mirrors sync_scripts.py exactly.

Uses PyYAML rather than ruamel.yaml deliberately: PyYAML ships with
Home Assistant Core itself (HA cannot run without it), so this script
is guaranteed to work in whatever environment `shell_command` actually
executes in, with no separate pip install and no risk of it going
missing after a future HA update. The trade-off: comments in your
YAML files are NOT preserved across a sync -- every write is a fresh
dump from the parsed data, not a round-trip edit.

How it decides direction (normal mode, no --force):
- Hashes the current content of both sides.
- Compares against the hashes recorded at the last successful sync
  (stored in .automations_sync_state.yaml).
- Whichever side's hash changed gets propagated to the other side.
- If BOTH changed since the last sync, this is a real conflict (you
  edited the same automations on both sides between syncs) -- the
  script does nothing to either file, prints a warning, and exits
  with code 2. Resolve manually (see --force below), then it'll sync
  cleanly next run.

Before touching anything (normal, forced, or bootstrap), both sides
are validated: every automation must have a unique id (or, failing
that, a unique alias). Automations are keyed by id/alias internally,
so a missing or duplicate key would otherwise cause one automation to
silently overwrite another on the next write. If validation fails,
nothing is written -- fix the offending automation(s) and re-run.

Usage:
    python3 sync_automations.py                # normal two-way sync
    python3 sync_automations.py --bootstrap    # ONE-TIME: split the
                                                # current
                                                # automations.yaml
                                                # into automations/
                                                # and record the
                                                # baseline. Refuses to
                                                # run if automations/
                                                # already has files
                                                # in it. Run this
                                                # once, manually,
                                                # before relying on
                                                # the scheduled
                                                # automation.
    python3 sync_automations.py --force flat   # conflict resolution:
                                                # skip the conflict
                                                # check entirely,
                                                # unconditionally
                                                # regenerate
                                                # automations/ from
                                                # the current
                                                # automations.yaml
                                                # ("local wins")
    python3 sync_automations.py --force split  # conflict resolution:
                                                # unconditionally
                                                # regenerate
                                                # automations.yaml
                                                # from the current
                                                # automations/
                                                # ("git wins")

Exit codes:
    0 - nothing to do, or synced cleanly (or forced flat->split), no
        reload needed
    1 - synced (or forced) split -> automations.yaml direction; HA
        needs automation.reload to pick it up
    2 - conflict detected, nothing changed, needs manual resolution
        (re-run with --force flat or --force split)
    3 - validation failed (missing or duplicate id/alias), no
        baseline recorded yet (normal mode only, run --bootstrap or
        --force first), or a bad argument -- nothing changed, needs
        manual resolution
"""
import hashlib
import re
import sys
from pathlib import Path

import yaml

CONFIG_DIR = Path("/config")
AUTOMATIONS_FILE = CONFIG_DIR / "automations.yaml"
SPLIT_DIR = CONFIG_DIR / "automations"
STATE_FILE = CONFIG_DIR / ".automations_sync_state.yaml"


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "automation"


def dump_to_str(data) -> str:
    return yaml.dump(
        data, default_flow_style=False, sort_keys=False, allow_unicode=True, width=4096
    )


def load_flat() -> list:
    if not AUTOMATIONS_FILE.exists():
        return []
    return yaml.safe_load(AUTOMATIONS_FILE.read_text()) or []


def load_split() -> list:
    automations = []
    if not SPLIT_DIR.exists():
        return automations
    for f in sorted(SPLIT_DIR.glob("*.yaml")):
        items = yaml.safe_load(f.read_text()) or []
        automations.extend(items)
    return automations


def validate(automations: list, source_name: str) -> list:
    """Return a list of human-readable problems, empty if all good.

    Every automation needs a stable, unique key (id, falling back to
    alias) -- write_flat/write_split/canonical_hash all key on this,
    so a missing or duplicate key means one automation would silently
    overwrite another.
    """
    problems = []
    seen = {}
    for i, a in enumerate(automations):
        key = a.get("id") or a.get("alias")
        if not key:
            problems.append(
                f"{source_name}: automation at position {i} has neither "
                f"'id' nor 'alias' -- cannot be tracked safely."
            )
            continue
        if key in seen:
            problems.append(
                f"{source_name}: duplicate key '{key}' used by both "
                f"'{seen[key]}' and this automation at position {i}."
            )
        seen[key] = a.get("alias", key)
    return problems


def canonical_hash(automations: list) -> str:
    """Order-independent hash keyed by automation id (falls back to alias)."""
    by_id = {}
    for a in automations:
        key = a.get("id") or a.get("alias")
        by_id[key] = a
    ordered = [by_id[k] for k in sorted(by_id.keys(), key=str)]
    return hashlib.sha256(dump_to_str(ordered).encode()).hexdigest()


def write_flat(automations: list):
    by_id = {}
    for a in automations:
        key = a.get("id") or a.get("alias")
        by_id[key] = a
    ordered = [by_id[k] for k in sorted(by_id.keys(), key=str)]
    AUTOMATIONS_FILE.write_text(dump_to_str(ordered))


def write_split(automations: list):
    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    # Clear existing split files -- flat side is authoritative for this direction.
    for f in SPLIT_DIR.glob("*.yaml"):
        f.unlink()

    seen_slugs = {}
    for a in automations:
        alias = a.get("alias") or a.get("id") or "automation"
        slug = slugify(alias)
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}_{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 0
        out_file = SPLIT_DIR / f"{slug}.yaml"
        out_file.write_text(dump_to_str([a]))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    return yaml.safe_load(STATE_FILE.read_text()) or {}


def save_state(flat_hash: str, split_hash: str):
    STATE_FILE.write_text(
        dump_to_str({"last_flat_hash": flat_hash, "last_split_hash": split_hash})
    )


def bootstrap():
    """One-time: split the current automations.yaml into automations/
    and record the baseline state. Refuses to run if automations/
    already has files in it, to avoid clobbering an existing split by
    accident. (If you need to force-overwrite an existing split, use
    --force flat instead -- it has no such guard rail.)"""
    existing_split_files = list(SPLIT_DIR.glob("*.yaml")) if SPLIT_DIR.exists() else []
    if existing_split_files:
        print(
            f"automations/ already contains {len(existing_split_files)} file(s) -- "
            "refusing to bootstrap over existing data. Delete/move them first, "
            "or use --force flat if you deliberately want to overwrite them.",
            file=sys.stderr,
        )
        sys.exit(3)

    flat_automations = load_flat()
    problems = validate(flat_automations, "automations.yaml")
    if problems:
        print("VALIDATION FAILED -- nothing changed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(3)

    if not flat_automations:
        print("automations.yaml is empty or missing -- nothing to bootstrap.")
        sys.exit(0)

    write_split(flat_automations)
    flat_hash = canonical_hash(flat_automations)
    split_hash = canonical_hash(load_split())
    save_state(flat_hash, split_hash)
    print(f"Bootstrapped: split {len(flat_automations)} automation(s) into {SPLIT_DIR}/")
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
            print("Usage: sync_automations.py [--bootstrap | --force flat|split]", file=sys.stderr)
            sys.exit(3)

    flat_automations = load_flat()
    split_automations = load_split()

    problems = validate(flat_automations, "automations.yaml") + validate(
        split_automations, "automations/"
    )
    if problems:
        print("VALIDATION FAILED -- nothing changed:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(3)

    current_flat_hash = canonical_hash(flat_automations)
    current_split_hash = canonical_hash(split_automations)

    if force == "flat":
        write_split(flat_automations)
        new_split_hash = canonical_hash(load_split())
        save_state(current_flat_hash, new_split_hash)
        print(
            f"FORCED automations.yaml -> automations/ "
            f"({len(flat_automations)} automations). automations/ edits discarded."
        )
        sys.exit(0)

    if force == "split":
        write_flat(split_automations)
        new_flat_hash = canonical_hash(load_flat())
        save_state(new_flat_hash, current_split_hash)
        print(
            f"FORCED automations/ -> automations.yaml "
            f"({len(split_automations)} automations). automations.yaml edits discarded."
        )
        sys.exit(1)

    state = load_state()
    last_flat_hash = state.get("last_flat_hash")
    last_split_hash = state.get("last_split_hash")

    if last_flat_hash is None and last_split_hash is None:
        print(
            "No baseline recorded yet. Run 'python3 sync_automations.py --bootstrap' "
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
            "CONFLICT: both automations.yaml and automations/ changed since "
            "the last sync. Not touching either file -- run with "
            "'--force flat' to keep automations.yaml, or '--force split' to "
            "keep automations/, then this will sync cleanly.",
            file=sys.stderr,
        )
        sys.exit(2)

    if flat_changed:
        write_split(flat_automations)
        new_split_hash = canonical_hash(load_split())
        save_state(current_flat_hash, new_split_hash)
        print(f"Synced automations.yaml -> automations/ ({len(flat_automations)} automations).")
        sys.exit(0)

    if split_changed:
        write_flat(split_automations)
        new_flat_hash = canonical_hash(load_flat())
        save_state(new_flat_hash, current_split_hash)
        print(f"Synced automations/ -> automations.yaml ({len(split_automations)} automations).")
        sys.exit(1)


if __name__ == "__main__":
    main()
