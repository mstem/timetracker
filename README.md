# Timetracker

Vibecoding and jumping between 12 different terminal windows (and browser tabs, and everything else) kind of obliterates traditional time tracking across projects. This little script hopes to return a little bit of automated-tracking sanity to your logs so you can get a sense of roughly where your time went when the hours pass.

Automatically tracks whichever Mac app/window you're actually using — Terminal, Chrome tabs (with URL), or anything else — and syncs daily totals to [Clockify](https://clockify.me). Runs as a macOS background daemon (launchd), polls every 2 seconds, and sends aggregated entries at midnight. New activity that hasn't been seen before is automatically classified into a Clockify project by Claude right before it's sent — you shouldn't need to hand-map most things.

**macOS only** — uses AppleScript/System Events to detect the frontmost app and window, and Chrome's own AppleScript dictionary for tab URLs. **Firefox isn't supported yet** (Firefox has no AppleScript tab/URL dictionary the way Chrome does — out of scope for now).

## How it works

1. Every 2 seconds, checks which app/window is frontmost (Terminal, Chrome, or anything else) and, for Chrome, the active tab's URL
2. Skips capture entirely for blocklisted apps/domains (see [Privacy / blocklist](#privacy--blocklist)), and pauses after a period of no mouse/keyboard input (see [Idle detection](#idle-detection))
3. Logs time-spans to a daily JSON file in `logs/`
4. Right before sending (at midnight rollover, on-demand, or on startup catch-up), any activity Claude hasn't classified yet gets matched to a Clockify project in one batch call — see [AI-powered project classification](#ai-powered-project-classification-optional)
5. Entries are aggregated per activity (a Terminal directory, a browser domain, or an app name) and sent to Clockify with a representative window/tab title as the description

## Requirements

- macOS (Terminal.app, Chrome optional)
- Python 3 (ships with macOS — no pip installs needed)
- A [Clockify](https://clockify.me) account and API key
- **Accessibility permission** for whatever process runs the tracker (see below) — needed for tracking apps other than Terminal/Chrome

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/timetracker.git
cd timetracker
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
- **project_id**: optional; used as a fallback when no per-activity mapping exists

See [Configuration reference](#configuration-reference) below for the rest of the keys (blocklist, idle detection, video exemptions, per-source minimum durations) — all optional, all with sensible defaults.

### 3. Grant Accessibility permission

Tracking apps other than Terminal/Chrome works by asking `System Events` for the frontmost app's window name, which requires **Accessibility** permission:

1. System Settings → Privacy & Security → Accessibility
2. Grant access to whatever process ends up running `osascript` — in practice this is usually **Terminal.app** (if you run `python3 tracker.py` from a Terminal window) and/or **`/usr/bin/python3`** once it's running under launchd. macOS often prompts automatically the first time the script tries; if it doesn't, add the binary manually.
3. Verify it worked: run `python3 tracker.py` in the foreground for a few seconds, switch to a non-Terminal, non-Chrome app, then check `tracker.log` — you should see `Activity focus: 'app:...'` lines, not `osascript system events error` ones.

### 4. Install as a login item (auto-starts on login)

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

# Map an activity key to a Clockify project
python3 tracker.py --map my-project-dir PROJECT_ID
python3 tracker.py --map chrome:github.com PROJECT_ID
python3 tracker.py --map app:Slack PROJECT_ID
```

## Project mappings

Every logged activity is reduced to an **activity key** before it's mapped or aggregated:

| Source | Example window/tab | Activity key |
|---|---|---|
| Terminal | `my-project — zsh` | `my-project` (part before ` — `) |
| Chrome | any tab on `https://github.com/...` | `chrome:github.com` |
| Any other app | Slack, Figma, TextEdit, etc. | `app:Slack` |

```bash
# See your projects and their IDs
python3 tracker.py --list-projects

# Associate an activity key with a project
python3 tracker.py --map my-project-dir abc123projectid
```

Mappings are saved in `project_mappings.json` (gitignored — stays local). In practice you shouldn't need to do this by hand much — see the next section.

## AI-powered project classification (optional)

Right before every send (midnight rollover, `--send-today`, `--send DATE`, or startup catch-up of missed days), the tracker looks at that day's entries for any activity key with no existing mapping, and asks Claude to classify all of them against your Clockify projects in **one batch API call** — using a few sample window/tab titles per key as context, not just the key string. Confident matches are saved to `project_mappings.json` and never re-queried; low-confidence keys are left unmapped and retried next time. A classification failure never blocks the send — entries that already have mappings still go through.

### Setup

1. Get an [Anthropic API key](https://console.anthropic.com/)
2. Add it to `config.json`:
   ```json
   { "anthropic_api_key": "sk-ant-..." }
   ```

That's it — no separate cron job to install. `ai_matcher.py` also works as a standalone CLI for manual review or corrections:

```bash
# Classify unmapped activity from the last 7 days right now (auto-saves confident matches)
python3 ai_matcher.py

# Preview matches without saving anything
python3 ai_matcher.py --dry-run

# Look back further
python3 ai_matcher.py --days 30

# Walk through each match interactively — confirm, override, or create new projects
python3 ai_matcher.py --interactive

# Link an activity key to a project by name — no AI needed, fuzzy match
python3 ai_matcher.py --link chrome:github.com "GitHub Project"
python3 ai_matcher.py --link app:Slack "Team Chat"
```

### Interactive mode

`--interactive` shows your full project list grouped by client, then walks through each unmapped activity key one at a time:

```
  ctfg-timeliness  →  VA CTFG
    [y/n/back/project name]: 
```

- `y` or Enter — accept the proposed match
- `n` — skip (will retry next run)
- `back` — go back and redo the previous key
- type a name — fuzzy-matches against your existing projects; if nothing matches, offers to **create a new Clockify project** on the spot

### How it works

- Scans log files for the last N days (default 7, or the specific day being sent)
- Extracts activity keys and a few sample titles per key (same logic the tracker uses)
- Sends all unmapped keys + samples + all your Clockify project names to Claude in **one API call**
- Claude returns a JSON match for each key; conservative — prefers `null` over a weak guess
- Confirmed matches are written to `project_mappings.json`
- Keys where Claude returns no match remain unmapped and are retried next time
- Two guards keep it conservative: domains in `generic_domains` (sign-in flows, bare search homepages — see [Configuration reference](#configuration-reference)) are never even sent to Claude, and your own already-confirmed mappings are included as calibration examples so it has a concrete sense of what "specific enough" looks like in your data, not just the raw project list

## Privacy / blocklist

Since activity is now tracked for *any* app, not just Terminal, add sensitive apps or domains to `config.json` so they're never logged, classified, or sent anywhere — blocked activity is treated exactly like "no active window":

```json
{
  "blocked_apps": ["1Password", "Messages", "Mail"],
  "blocked_domains": ["chase.com", "mybank.com"]
}
```

## Idle detection

After `idle_threshold_seconds` (default 600 = 10 min) of no mouse/keyboard input, the current session is closed off — backdated to the moment input actually stopped, not the moment idle was detected, so the logged duration doesn't include dead time. No new activity is captured while idle.

**Exception:** if the focused app/domain is in `video_apps`/`video_domains` (Zoom, FaceTime, YouTube, Netflix, etc. by default), idle detection is skipped — watching a video or being on a call doesn't require touching the mouse or keyboard. One tradeoff worth knowing: if you leave a paused or finished video frontmost and walk away, that time keeps accruing since there's no way to distinguish "still watching" from "forgot to close the tab."

## Configuration reference

All keys below are optional and have built-in defaults — omit any you don't need to change:

```json
{
  "blocked_apps": [],
  "blocked_domains": [],
  "video_apps": ["zoom.us", "FaceTime", "QuickTime Player", "VLC"],
  "video_domains": ["youtube.com", "netflix.com", "vimeo.com", "twitch.tv", "meet.google.com", "zoom.us"],
  "idle_threshold_seconds": 600,
  "min_duration_seconds": {"terminal": 1, "chrome": 60, "app": 60},
  "generic_domains": ["accounts.google.com", "myaccount.google.com", "login.microsoftonline.com", "okta.com"]
}
```

- **`min_duration_seconds`**: how long an activity must stay focused before it's logged at all, per source. Terminal logs almost instantly; Chrome and other apps default to 60s to avoid noise from quick tab/window flicks.
- **`generic_domains`**: Chrome domains that are pure auth infrastructure — a sign-in screen never carries project signal, so these are never even sent to Claude. These are exact matches (unlike `blocked_domains`/`video_domains`, which also match subdomains), so `accounts.google.com` won't swallow `docs.google.com` or `calendar.google.com` — those still get classified normally when the tab title names something specific. Search engines (`google.com`, etc.) are deliberately **not** in this list by default — their titles can carry real signal — but note the tradeoff: the mapping key is per-domain, so a single confident match caches and applies to every future search on that domain too, including unrelated ones. Add search domains back to `generic_domains` yourself if that risk isn't worth it for you.

## Watchdogs (optional)

Since the tracker runs silently in the background, it's easy to not notice if it stops working. Two launchd agents alert you via a Telegram bot when something's wrong:

- **`watchdog_process.sh`** (every 10 min): checks whether the `com.terminaltracker` launchd job is actually running; alerts if it crashed or got unloaded.
- **`watchdog_health.sh`** (hourly, 9am-8pm): checks that today's log file has been written to recently. Catches the case where the process is alive but silently stuck (e.g. it lost AppleScript/Accessibility permission) — something a simple "is it running" check would miss.

### Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) (`/newbot`) and note the token it gives you
2. Send your bot any message, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`
3. Add both to `config.json`:
   ```json
   { "telegram_bot_token": "123456:ABC...", "telegram_chat_id": "123456789" }
   ```
4. Re-run `setup.sh` — it installs both watchdog agents

### Manual usage

```bash
# Send a test message
./notify_telegram.sh "test message"

# Run a watchdog check manually
./watchdog_process.sh
./watchdog_health.sh
```

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
            "command": "python3 /path/to/timetracker/check_clockify_mapping.py"
          }
        ]
      }
    ]
  }
}
```

## File layout

```
timetracker/
├── tracker.py                 # main daemon: capture, idle detection, aggregation, sending
├── ai_matcher.py               # Claude-based project classifier (CLI + importable classify_and_save)
├── common.py                   # shared config/Clockify/Anthropic/mapping helpers
├── check_clockify_mapping.py  # optional Claude Code hook
├── notify_telegram.sh         # shared Telegram-send helper
├── watchdog_process.sh        # alerts if the tracker daemon isn't running
├── watchdog_health.sh         # alerts if today's log has gone stale
├── setup.sh                   # installs the launchd agents
├── config.example.json        # template — copy to config.json and fill in
└── logs/                      # daily JSON logs (auto-created, gitignored)
```

## Logs

```bash
tail -f tracker.log          # daemon activity
cat logs/2025-03-15.json     # raw entries for a specific day
```
