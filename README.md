# Home Assistant Github sync for Automations & Scripts

This is a small set of scripts that keeps your Home Assistant **automations** and **scripts** backed up to GitHub automatically, in a way that's easy to browse, easy to review, and — if you want — easy to let an AI assistant edit directly on GitHub.

It was built for one specific setup, not as a general-purpose plugin, so read through this before installing it. It assumes you're reasonably comfortable copying and pasting terminal commands, even if you're not a programmer.

## Table of contents

- [What this does](#what-this-does)
- [How it works](#how-it-works)
- [What's in this repo](#whats-in-this-repo)
- [Requirements](#requirements)
- [Setup guide](#setup-guide)
- [Everyday use](#everyday-use)
- [When there's a conflict](#when-theres-a-conflict)
- [Deleting an automation or script](#deleting-an-automation-or-script)
- [Troubleshooting](#troubleshooting)
- [Good to know / limitations](#good-to-know--limitations)

## What this does

Home Assistant normally stores every automation you create in one giant file (`automations.yaml`), and every script in another (`scripts.yaml`). That's fine for Home Assistant, but it's awkward if you want to:

- Keep a proper backup history on GitHub, so you can see exactly what changed and when.
- Look at a clean history of *one automation at a time*, instead of one giant file where everything is mixed together.
- Let an AI assistant (or a collaborator) edit individual automations or scripts as separate files on GitHub, without wading through everything else.

This project splits your automations and scripts into **one file per automation/script**, keeps that split copy in sync with GitHub, and automatically merges any changes back into Home Assistant. It runs on a schedule (once an hour, by default) with no ongoing effort from you.

You do not need to understand git deeply to use this — the setup steps below explain everything you need to type.

## How it works

There are two things being synced, and they behave slightly differently on purpose:

| | Automations | Scripts |
|---|---|---|
| **Local file(s) Home Assistant actually reads** | The split files, one per automation | One single `scripts.yaml` |
| **Can you edit it from the Home Assistant UI?** | No — see note below | Yes, exactly as normal |
| **What GitHub sees** | The split files | The split files |

**Why the difference?** Home Assistant's own automation/script editor only lets you use the visual "click to edit" interface when everything lives in one file. For automations, we accepted the trade-off of losing that visual editor in exchange for clean, split files. For scripts, there's a trick that avoids the trade-off entirely: the split files only exist *temporarily*, purely to push to GitHub — locally, Home Assistant only ever sees `scripts.yaml`, so the UI editor keeps working normally.

Either way, from GitHub's point of view, both automations and scripts show up as clean, individual files — easy to read, easy to diff, easy for an AI to edit one at a time.

**The sync runs in both directions.** If you edit something locally (through the UI, for scripts) or an AI edits a file directly on GitHub, the next sync picks it up and applies it on the other side automatically.

## What's in this repo

| File | What it does |
|---|---|
| `sync_automations.py` | Keeps `automations.yaml` and the split `automations/` files in agreement with each other. |
| `sync_scripts.py` | Same idea, for `scripts.yaml` and a *temporary* `scripts/` folder. |
| `git_sync.sh` | The main script. Talks to GitHub, decides what needs to happen, and calls the two Python scripts above. This is the one thing you actually run or schedule. |

## Requirements

- A working Home Assistant installation, with some way to run terminal commands against its `/config` folder. Any of these work:
  - The **Terminal & SSH** add-on
  - **Blueprint Studio**'s built-in terminal (or a similar file-manager add-on with terminal access)
  - SSH access to your Home Assistant machine directly
- A free **GitHub** account, and a repository to sync into (can be private).
- `git` available in that terminal — it comes pre-installed with the add-ons above.
- Python 3 with the `PyYAML` package — this ships with Home Assistant itself, so nothing extra to install.

## Setup guide

Work through these in order. Each step assumes the ones before it are done.

### 1. Create a GitHub repository

If you don't already have one, create a new (private is fine) repository on GitHub to hold your Home Assistant config.

### 2. Get a GitHub access token

Home Assistant needs permission to push to your repository without you typing a password every time.

1. Go to **github.com → Settings → Developer settings → Personal access tokens → Tokens (classic)**.
2. Generate a new token with the **`repo`** scope.
3. Copy it somewhere safe — you'll need it in the next step, and GitHub won't show it to you again.

### 3. Connect your repository to `/config`

Open a terminal against your Home Assistant `/config` folder, then:

```bash
cd /config
git init                     # skip this if /config is already a git repo
git remote add origin https://<YOUR-TOKEN>@github.com/<your-username>/<your-repo>.git
```

Replace `<YOUR-TOKEN>`, `<your-username>`, and `<your-repo>` with your own details.

### 4. Copy the three files in

Place `sync_automations.py`, `sync_scripts.py`, and `git_sync.sh` directly inside `/config` — **not** inside a subfolder like `/config/automations/` or `/config/scripts/**`, since those folders get automatically managed (and in the case of `scripts/`, periodically deleted and recreated) by the sync process itself.

```bash
chmod +x /config/git_sync.sh
```

### 5. Add the required `.gitignore` entries

Create or open `/config/.gitignore` and make sure it includes:

```
.automations_sync_state.yaml
.automations_sync.lock/
.scripts_sync_state.yaml
automations.yaml
scripts.yaml
```

These are bookkeeping files the scripts use locally — none of them should ever end up on GitHub.

> If this repository also holds the rest of your Home Assistant configuration, also make sure your recorder database (`home-assistant_v2.db`), `.storage/`, and any `custom_components/` you installed through HACS are excluded too. Those are outside the scope of this tool, but a very common trip-up if you're setting up git for Home Assistant for the first time.

### 6. Point Home Assistant at the split automation files

Open `configuration.yaml` and make sure your automations line reads:

```yaml
automation: !include_dir_merge_list automations/
```

**Leave your `script:` setting exactly as it already is.** Scripts are meant to keep loading from the single `scripts.yaml` file — that's the whole point of the design.

### 7. Do the one-time split of your scripts

This takes whatever's currently in `scripts.yaml` and creates the initial split files:

```bash
cd /config
python3 sync_scripts.py --bootstrap
```

You should see a message confirming how many scripts were split. (Automations don't need an equivalent step here — if you're starting fresh with this tool, an equivalent one-time split for automations should be done before pointing `configuration.yaml` at the folder in step 6.)

### 8. Add the shell command

In `configuration.yaml`:

```yaml
shell_command:
  git_sync: "bash /config/git_sync.sh"
```

Restart Home Assistant, or reload just this section via **Developer Tools → YAML**.

### 9. Add the scheduled sync

Go to **Settings → Automations & Scenes → Create Automation → Edit in YAML**, and paste:

```yaml
alias: Hourly automation/script sync
description: ""
triggers:
  - trigger: time_pattern
    hours: "/1"
conditions: []
actions:
  - action: shell_command.git_sync
    response_variable: result
  - choose:
      - conditions:
          - condition: template
            value_template: "{{ 'LOCK_BUSY' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync skipped"
              message: "A sync was already running; this run was skipped."
      - conditions:
          - condition: template
            value_template: "{{ 'GIT_FETCH_FAILED' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync failed"
              message: "Couldn't reach GitHub."
      - conditions:
          - condition: template
            value_template: "{{ 'GIT_MERGE_FAILED' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync failed"
              message: "Couldn't merge changes from GitHub. Check /config manually."
      - conditions:
          - condition: template
            value_template: "{{ 'DIVERGED_MERGED_CLEAN' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync: merged automatically"
              message: "Local and GitHub had both changed, but touched different files — merged cleanly."
      - conditions:
          - condition: template
            value_template: "{{ 'DIVERGED_RESOLVED=local' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync: conflict auto-resolved"
              message: "The same file was edited on both sides. The local version was newer and was kept."
      - conditions:
          - condition: template
            value_template: "{{ 'DIVERGED_RESOLVED=remote' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Sync: conflict auto-resolved"
              message: "The same file was edited on both sides. The GitHub version was newer and was kept."
      - conditions:
          - condition: template
            value_template: "{{ 'exit_code=1' in result.stdout }}"
        sequence:
          - action: automation.reload
      - conditions:
          - condition: template
            value_template: "{{ 'exit_code=2' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Automation conflict"
              message: "Both sides changed the same automation since the last sync. See the 'When there's a conflict' section of the README."
      - conditions:
          - condition: template
            value_template: "{{ 'exit_code=3' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Automation validation failed"
              message: "An automation is missing an id/alias, or two share one. Nothing was synced."
      - conditions:
          - condition: template
            value_template: "{{ 'scripts_exit_code=1' in result.stdout }}"
        sequence:
          - action: script.reload
      - conditions:
          - condition: template
            value_template: "{{ 'scripts_exit_code=2' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Script conflict"
              message: "Both sides changed the same script since the last sync. See the 'When there's a conflict' section of the README."
      - conditions:
          - condition: template
            value_template: "{{ 'scripts_exit_code=3' in result.stdout }}"
        sequence:
          - action: persistent_notification.create
            data:
              title: "Script sync failed"
              message: "Either two split files define the same script id, or scripts haven't been bootstrapped yet."
mode: single
```

### 10. Add manual controls (recommended)

These give you buttons you can tap on a dashboard instead of waiting for the hourly schedule.

```yaml
shell_command:
  git_sync: "bash /config/git_sync.sh"
  resolve_automations_local: "bash /config/git_sync.sh --resolve automations local"
  resolve_automations_git: "bash /config/git_sync.sh --resolve automations git"
  resolve_scripts_local: "bash /config/git_sync.sh --resolve scripts local"
  resolve_scripts_git: "bash /config/git_sync.sh --resolve scripts git"
```

```yaml
script:
  sync_now:
    alias: "Sync now"
    sequence:
      - action: shell_command.git_sync

  resolve_automations_keep_local:
    alias: "Automations: keep local, discard GitHub"
    sequence:
      - action: shell_command.resolve_automations_local
      - action: persistent_notification.create
        data:
          title: "Resolved"
          message: "Kept your local automation — GitHub was updated to match."

  resolve_automations_keep_git:
    alias: "Automations: keep GitHub, discard local"
    sequence:
      - action: shell_command.resolve_automations_git
      - action: automation.reload
      - action: persistent_notification.create
        data:
          title: "Resolved"
          message: "Kept GitHub's version and reloaded it."

  resolve_scripts_keep_local:
    alias: "Scripts: keep local, discard GitHub"
    sequence:
      - action: shell_command.resolve_scripts_local
      - action: persistent_notification.create
        data:
          title: "Resolved"
          message: "Kept your local script — GitHub was updated to match."

  resolve_scripts_keep_git:
    alias: "Scripts: keep GitHub, discard local"
    sequence:
      - action: shell_command.resolve_scripts_git
      - action: script.reload
      - action: persistent_notification.create
        data:
          title: "Resolved"
          message: "Kept GitHub's version and reloaded it."
```

Add these as button cards on any dashboard for one-tap access.

### 11. Test it

Run a sync manually and read the output:

```bash
cd /config
bash git_sync.sh
```

You should see something like `git_relation=same` and `exit_code=0` / `scripts_exit_code=0` with no errors. Then check:

- On GitHub, your repository should show `automations/` and `scripts/` folders full of individual files.
- Locally, `ls /config` should show `scripts.yaml` but **no `scripts/` folder** — that only appears briefly while a sync is running.

If that all looks right, you're done. Let the hourly automation run on its own for a while, and check back that it's firing without errors.

## Everyday use

Day to day, you don't need to do anything. The hourly automation handles it:

- If you edited a script through the Home Assistant UI, it gets split and pushed to GitHub.
- If a script or automation was changed directly on GitHub (by you, or by an AI you've pointed at the repo), it gets pulled down and applied to your running Home Assistant.
- If both happened at once but touched *different* automations or scripts, both changes are kept — nothing is lost.

You'll get a notification in Home Assistant any time something noteworthy happens (a sync failed, a conflict was found, etc.). No notification means everything is fine.

## When there's a conflict

A conflict means the **same** automation or script was changed both locally and on GitHub since the last successful sync, and the tool can't safely guess which version you want to keep — so it leaves both untouched and asks you to decide.

You'll see a notification for this ("Automation conflict" or "Script conflict"). To resolve it, use the buttons from step 10 above, or run one of these directly:

```bash
# Keep the version currently on Home Assistant, discard what's on GitHub
bash /config/git_sync.sh --resolve automations local
bash /config/git_sync.sh --resolve scripts local

# Keep the version on GitHub, discard the local one
bash /config/git_sync.sh --resolve automations git
bash /config/git_sync.sh --resolve scripts git
```

This immediately fixes the conflict and re-runs a full sync, so there's nothing else to do afterward.

There's a second, unrelated situation that looks similar but resolves itself automatically: if you and GitHub both made *separate commits* (not just edits) since the last sync, but touched *different* files, the tool merges them on its own with no data lost (`DIVERGED_MERGED_CLEAN`). If they touched the *same* file, it picks whichever change is newer and discards the other, and tells you which one it picked (`DIVERGED_RESOLVED=local` or `=remote`). You don't need to do anything for this case, but it's worth a glance if you see the notification, just to confirm the right side won.

## Deleting an automation or script

Delete the individual file (either directly on GitHub, or locally in the `automations/` folder — scripts don't have a persistent local folder to delete from, so delete it on GitHub instead, or edit `scripts.yaml` directly and let the sync catch up). The next sync picks up the deletion and removes it from Home Assistant automatically.

**Don't rely on Home Assistant's own "Delete" button in the automations list** for automations — since they're split into individual files, it isn't confirmed to behave correctly. Deleting the file directly is the reliable way.

## Troubleshooting

**"A sync was already running; this run was skipped."**
Normal — another sync was in progress. It'll run again in an hour, or you can tap "Sync now."

**Push fails asking for a username and password.**
GitHub no longer accepts plain passwords for this. Make sure your token is embedded in the remote URL as shown in step 3, not typed in manually when prompted.

**`ModuleNotFoundError: No module named 'yaml'` or similar.**
This shouldn't happen — the scripts intentionally use the `PyYAML` package that ships with Home Assistant itself. If you see this, something about your Home Assistant installation is unusual; double check you're running the command as the same user/environment Home Assistant itself uses (test via **Developer Tools → Actions → `shell_command.git_sync`**, not just a manual terminal command).

**An automation says "This automation cannot be edited from the UI."**
Expected — see [How it works](#how-it-works) above. Edit the split file directly, or on GitHub, instead.

**A sync keeps reporting `scripts_exit_code=3` and mentions "no baseline recorded."**
You haven't run the one-time bootstrap step yet — see step 7.

## Good to know / limitations

- **Comments in your YAML are not preserved.** Every sync rewrites the affected files fresh from the parsed data. If you rely on comments for notes, they won't survive a sync.
- **`automations.yaml` is not actually used by Home Assistant** once you complete step 6 — it exists purely as internal bookkeeping for the sync scripts and is excluded from GitHub. Don't be surprised that editing it directly does nothing.
- **This was built for one specific setup and workflow.** It works well for that, but it isn't a polished, general-purpose tool — expect to adjust it if your needs differ meaningfully from what's described here.
