#!/bin/bash
# git_sync.sh
#
# NOTE: this file must NOT live inside /config/scripts/ -- that
# directory is the transient split target for scripts.yaml (see
# below) and gets rm -rf'd at the end of most runs. Keep this script
# directly in /config/ (or anywhere outside /config/scripts/ and
# /config/automations/).
#
# Usage:
#   bash git_sync.sh
#       Normal run: fetch, reconcile both domains, commit, push.
#       Called by the hourly automation and the manual "sync now"
#       button.
#
#   bash git_sync.sh --resolve <automations|scripts> <local|git>
#       Manual conflict resolution. Use when a previous run reported
#       exit_code=2 (automations) or scripts_exit_code=2 (scripts).
#         local -> keep the flat file (automations.yaml / scripts.yaml,
#                  whatever's live in HA / the UI right now), discard
#                  whatever's different on the split/GitHub side.
#         git   -> keep the split files (whatever's on GitHub right
#                  now), discard whatever's different in the flat
#                  file.
#       Runs the normal fetch/merge/reconcile/push flow immediately
#       afterward, so this is a one-shot "resolve and re-sync."
#
# Covers two domains, handled differently on purpose:
#
#   automations.yaml <-> automations/
#     Both persist permanently on local disk. configuration.yaml
#     points at automations/ via !include_dir_merge_list, so HA
#     itself reads the split files directly. Trade-off (accepted
#     earlier): automations can't be edited from the UI, since HA
#     only supports UI-editing when everything's in one file.
#
#   scripts.yaml <-> scripts/
#     Only scripts.yaml persists locally between runs. scripts/ is
#     materialized on disk ONLY for the duration of this script's
#     run (restored from the last commit at the start, deleted again
#     at the end -- UNLESS this run ended in a conflict or validation
#     failure, in which case scripts/ is left in place so there's
#     something to inspect/resolve) -- purely so it can be committed
#     and pushed to GitHub. configuration.yaml never references
#     scripts/ at all, so HA only ever reads scripts.yaml and
#     UI-editing scripts keeps working normally, with zero
#     special-casing.
#
# Git-level pull/merge/divergence handling is domain-agnostic (one
# repo, one fetch, one merge-base check) and unaffected by any of the
# above, including --resolve mode.
#
#   - Local and remote hash match          -> just reconcile working
#                                              files, commit/push if
#                                              anything changed.
#   - Remote is ahead (fast-forward)       -> pull, then reconcile.
#   - Local is ahead (fast-forward)        -> reconcile, then push.
#   - Diverged (neither is an ancestor)    -> both sides committed
#                                              independently. A real
#                                              git merge is attempted
#                                              first (safe for edits
#                                              to different files);
#                                              only a genuine same-
#                                              file conflict falls
#                                              back to a timestamp-
#                                              based override, which
#                                              discards the older
#                                              side's commit entirely.
#                                              This is a DIFFERENT
#                                              kind of conflict from
#                                              the working-file one
#                                              --resolve is for --
#                                              this one resolves
#                                              itself automatically.
#
# Always exits 0 -- status is communicated via stdout text:
#   LOCK_BUSY                     - another sync was already running, skipped
#   GIT_FETCH_FAILED              - couldn't reach GitHub, nothing else ran
#   GIT_MERGE_FAILED              - fast-forward merge from remote failed;
#                                   nothing else ran
#   GIT_RESOLVE_BAD_ARGS           - --resolve given with a bad domain or
#                                   direction; nothing ran
#   DIVERGED_MERGED_CLEAN         - both sides had independent commits,
#                                   but touched different files -- merged
#                                   automatically, nothing lost
#   DIVERGED_RESOLVED=local       - genuine content conflict; local was
#                                   newer, force-pushed over remote
#   DIVERGED_RESOLVED=remote      - genuine content conflict; remote was
#                                   newer, local's conflicting commit(s)
#                                   discarded
#   exit_code=0/1/2/3             - automations result (see
#                                   sync_automations.py's docstring)
#   scripts_exit_code=0/1/2/3     - scripts result (see
#                                   sync_scripts.py's docstring)

cd /config || { echo "ERROR: cannot cd to /config"; exit 0; }

RESOLVE_DOMAIN=""
RESOLVE_DIRECTION=""

if [ "$1" = "--resolve" ]; then
    if [ "$2" != "automations" ] && [ "$2" != "scripts" ]; then
        echo "GIT_RESOLVE_BAD_ARGS"
        exit 0
    fi
    if [ "$3" != "local" ] && [ "$3" != "git" ]; then
        echo "GIT_RESOLVE_BAD_ARGS"
        exit 0
    fi
    RESOLVE_DOMAIN="$2"
    RESOLVE_DIRECTION="$3"
fi

LOCKDIR="/config/.automations_sync.lock"
STALE_SECONDS=600

if [ -d "$LOCKDIR" ]; then
    lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKDIR" 2>/dev/null || echo 0) ))
    if [ "$lock_age" -gt "$STALE_SECONDS" ]; then
        rmdir "$LOCKDIR" 2>/dev/null
    fi
fi

if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo "LOCK_BUSY"
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT

# Restore scripts/ from the last commit (undoes the previous run's
# cleanup, or brings it back if a prior conflict left it in place) so
# the working tree is clean before we fetch/merge. Harmless no-op if
# scripts/ isn't tracked yet. automations/ needs no equivalent step --
# it's never deleted between runs.
git checkout HEAD -- scripts/ 2>/dev/null

if ! git fetch origin main; then
    echo "GIT_FETCH_FAILED"
    exit 0
fi

LOCAL_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/main)

if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
    RELATION="same"
elif git merge-base --is-ancestor HEAD origin/main; then
    RELATION="remote_ahead"
elif git merge-base --is-ancestor origin/main HEAD; then
    RELATION="local_ahead"
else
    RELATION="diverged"
fi

echo "git_relation=$RELATION"

if [ "$RELATION" = "diverged" ]; then
    if git merge origin/main -m "Merge remote changes"; then
        echo "DIVERGED_MERGED_CLEAN"
        RELATION="same"
    else
        git merge --abort

        LOCAL_TIME=$(git log -1 --format=%ct HEAD)
        REMOTE_TIME=$(git log -1 --format=%ct origin/main)

        if [ "$LOCAL_TIME" -ge "$REMOTE_TIME" ]; then
            echo "DIVERGED_RESOLVED=local"
            git push --force
        else
            echo "DIVERGED_RESOLVED=remote"
            git reset --hard origin/main
        fi
        RELATION="same"
    fi
fi

if [ "$RELATION" = "remote_ahead" ]; then
    if ! git merge --ff-only origin/main; then
        echo "GIT_MERGE_FAILED"
        exit 0
    fi
fi

# Reconcile automations.yaml <-> automations/. Normally two-way; if
# --resolve automations was requested, force the chosen direction
# instead ("local" = keep automations.yaml = force flat, "git" = keep
# automations/ = force split).
if [ "$RESOLVE_DOMAIN" = "automations" ]; then
    if [ "$RESOLVE_DIRECTION" = "local" ]; then
        python3 sync_automations.py --force flat
    else
        python3 sync_automations.py --force split
    fi
else
    python3 sync_automations.py
fi
sync_exit=$?
echo "exit_code=$sync_exit"

# Same for scripts.
if [ "$RESOLVE_DOMAIN" = "scripts" ]; then
    if [ "$RESOLVE_DIRECTION" = "local" ]; then
        python3 sync_scripts.py --force flat
    else
        python3 sync_scripts.py --force split
    fi
else
    python3 sync_scripts.py
fi
scripts_exit=$?
echo "scripts_exit_code=$scripts_exit"

git add automations/ scripts/
if ! git diff --cached --quiet; then
    git commit -m "Automation/script sync $(date +%F_%T)"
    git push
elif [ "$RELATION" = "local_ahead" ]; then
    git push
fi

# Remove scripts/ from local disk so /config only shows scripts.yaml
# between runs -- UNLESS this run ended in a conflict (2) or
# validation failure (3), in which case leave it in place so there's
# something to inspect. (A --resolve run always ends in 0 or 1, so
# cleanup always proceeds after a successful resolve.)
if [ "$scripts_exit" != "2" ] && [ "$scripts_exit" != "3" ]; then
    rm -rf /config/scripts
fi

exit 0
