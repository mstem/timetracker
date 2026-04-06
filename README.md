# Terminal Time Tracker

Vibecoding and jumping between 12 different terminal windows kind of obliterates traditional time tracking across projects. This little script hopes to return a little bit of automated-tracking sanity to your logs so you can get a sense of roughly where your time went when the hours pass.

It triggers only after at least 5 seconds in a window, so it will only catch project entries where you did more than hit enter blindly without thinking.

Automatically tracks time spent in Terminal.app windows and syncs daily totals to [Clockify](https://clockify.me). Runs as a macOS background daemon (launchd), polls every 2 seconds, and sends aggregated entries at midnight.

**macOS only** — uses AppleScript to detect the active Terminal window.

## How it works

1. Every 2 seconds, checks which Terminal window is in focus
2. Logs time-spans to a daily JSON file in `logs/`
3. At midnight (or on next startup if the daemon was off), aggregates and sends entries to Clockify
4. Window titles are used as entry descriptions; you can map directory names to specific Clockify projects

## Requirements

- macOS with Terminal.app
- Python 3 (ships with macOS — no pip installs needed)
- A [Clockify](https://clockify.me) account and API key

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/terminal-time-tracker.git
cd terminal-time-tracker
```

### 2. Create your config

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "api_key": "your-clockify-api-key",
  "workspace_id": "your-workspace-id",
  "project_id": "optional-default-project-id"
}
```

- **API key**: Clockify → Profile Settings → API → Generate
- **Workspace ID**: Clockify → Settings → copy from the URL (`clockify.me/workspaces/WORKSPACE_ID/...`)
- **project_id**: optional; used as a fallback when no per-directory mapping exists

### 3. Install as a login item (auto-starts on login)

```bash
bash setup.sh
```

This generates and loads a launchd agent. The tracker starts immediately and will restart automatically on login.

**Stop / start manually:**

```bash
launchctl unload ~/Library/LaunchAgents/com.terminaltracker.plist
launchctl load  ~/Library/LaunchAgents/com.terminaltracker.plist
```

## Usage

```bash
# Run manually (foreground, useful for testing)
python3 tracker.py

# Send today's log to Clockify right now
python3 tracker.py --send-today

# Send a specific past date
python3 tracker.py --send 2025-03-15

# List all Clockify projects (shows IDs)
python3 tracker.py --list-projects

# Map a directory name to a Clockify project
python3 tracker.py --map my-project-dir PROJECT_ID
```

## Project mappings

Terminal window titles typically look like `my-project — zsh`. The tracker extracts the part before ` — ` and uses it to look up a Clockify project.

```bash
# See your projects and their IDs
python3 tracker.py --list-projects

# Associate a directory name with a project
python3 tracker.py --map my-project-dir abc123projectid
```

Mappings are saved in `project_mappings.json` (gitignored — stays local).

## AI-powered project matching (optional)

`ai_matcher.py` scans your recent logs, finds directory names with no Clockify mapping, and asks Claude to match them against your projects — all in one batch API call. Matched results are saved to `project_mappings.json` and never re-queried.

### Setup

1. Get an [Anthropic API key](https://console.anthropic.com/)
2. Add it to `config.json`:
   ```json
   { "anthropic_api_key": "sk-ant-..." }
   ```
3. Re-run `setup.sh` — it will install a weekly launchd agent (every Monday at 9:00am).

### Manual usage

```bash
# Match unmatched dirnames from the last 7 days
python3 ai_matcher.py

# Preview matches without saving anything
python3 ai_matcher.py --dry-run

# Look back further
python3 ai_matcher.py --days 30

# Propose each match and confirm interactively (y/n or type a project name)
python3 ai_matcher.py --interactive

# Link a dirname to a project by name — no AI needed, fuzzy match
python3 ai_matcher.py --link my-project-dir "My Project Name"
```

### How it works

- Scans log files for the last N days (default 7)
- Extracts directory names from window titles (same logic as the tracker)
- Sends all unmatched names + all your Clockify project names to Claude in **one API call**
- Claude returns a JSON match for each dirname; conservative — prefers `null` over a weak guess
- Confirmed matches are written to `project_mappings.json`
- Dirnames where Claude returns no match remain unmatched and are retried on the next weekly run

## Claude Code hook (optional)

`check_clockify_mapping.py` is a Claude Code `UserPromptSubmit` hook. When you open a project in Claude Code that has no Clockify mapping yet, it prompts Claude to ask you which project to associate before proceeding.

To enable it, add to your Claude Code settings (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/terminal-time-tracker/check_clockify_mapping.py"
          }
        ]
      }
    ]
  }
}
```

## File layout

```
terminal-time-tracker/
├── tracker.py                 # main daemon
├── check_clockify_mapping.py  # optional Claude Code hook
├── setup.sh                   # installs the launchd agent
├── config.example.json        # template — copy to config.json and fill in
└── logs/                      # daily JSON logs (auto-created, gitignored)
```

## Logs

```bash
tail -f tracker.log          # daemon activity
cat logs/2025-03-15.json     # raw entries for a specific day
```
