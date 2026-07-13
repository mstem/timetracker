# Timetracker

Vibecoding and jumping between 12 different terminal windows (and browser tabs, and everything else) kind of obliterates traditional time tracking across projects. This little script hopes to return a little bit of automated-tracking sanity to your logs so you can get a sense of roughly where your time went when the hours pass.

Automatically tracks whichever Mac app/window you're actually using — Terminal, Chrome tabs (with URL), or anything else — and syncs daily totals to [Kimai](https://www.kimai.org). Runs as a macOS background daemon (launchd), polls every 2 seconds, and sends aggregated entries at midnight. New activity that hasn't been seen before is automatically classified into a Kimai project by Claude right before it's sent — you shouldn't need to hand-map most things.

It also syncs **external calendar meetings** to Kimai: any Google Calendar event with a guest who isn't you gets logged automatically (see [Calendar meeting sync](#calendar-meeting-sync)).

> **Migrating from Clockify?** This project previously targeted Clockify. See [Migrating from Clockify](#migrating-from-clockify) for the one-shot script that mirrors your clients/projects into Kimai and rewrites your mappings.

**macOS only** — uses AppleScript/System Events to detect the frontmost app and window, and Chrome's own AppleScript dictionary for tab URLs. **Firefox isn't supported yet** (Firefox has no AppleScript tab/URL dictionary the way Chrome does — out of scope for now).

## How it works

1. Every 2 seconds, checks which app/window is frontmost (Terminal, Chrome, or anything else) and, for Chrome, the active tab's URL
2. Skips capture entirely for blocklisted apps/domains (see [Privacy / blocklist](#privacy--blocklist)), and pauses after a period of no mouse/keyboard input (see [Idle detection](#idle-detection))
3. Logs time-spans to a daily JSON file in `logs/`
4. Right before sending (at midnight rollover, on-demand, or on startup catch-up), any activity Claude hasn't classified yet gets matched to a Kimai project in one batch call — see [AI-powered project classification](#ai-powered-project-classification-optional)
5. Entries are aggregated per activity (a Terminal directory, a browser domain, or an app name) and sent to Kimai with a representative window/tab title as the description

## Requirements

- macOS (Terminal.app, Chrome optional)
- Python 3.9+ (ships with macOS — no pip installs needed; uses stdlib `zoneinfo`)
- A [Kimai](https://www.kimai.org) instance and an API token (Kimai → User → API access)
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
  "kimai_url": "https://kimai.example.com",
  "kimai_token": "your-kimai-api-token",
  "kimai_timezone": "America/New_York",
  "default_project_id": null,
  "default_activity_id": null
}
```

- **kimai_url**: your Kimai base URL (no trailing `/api`)
- **kimai_token**: Kimai → User → API access → create a token; sent as a Bearer token
- **kimai_timezone**: the timezone your Kimai user is configured for. Kimai stores timesheet begin/end as local wall-clock time, so the tracker converts its UTC timestamps into this zone before sending. Use an IANA name (`America/New_York`, `Europe/Amsterdam`, …).
- **default_project_id / default_activity_id**: optional fallback used when no per-activity mapping exists. Kimai requires **both** a project *and* an activity on every timesheet — an entry with neither a mapping nor these defaults is skipped and retried later.

See [Configuration reference](#configuration-reference) below for the rest of the keys (blocklist, idle detection, video exemptions, per-source minimum durations) — all optional, all with sensible defaults.

> **Tip:** the [migration script](#migrating-from-clockify) prints ready-to-paste `default_*` and `meetings_*` ids after it mirrors your workspace, so you usually don't fill these in by hand.

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

# Send today's log to Kimai right now
python3 tracker.py --send-today

# Send a specific past date
python3 tracker.py --send 2025-03-15

# List all Kimai projects (shows IDs)
python3 tracker.py --list-projects

# List all Kimai activities (shows IDs)
python3 tracker.py --list-activities

# Map an activity key to a Kimai project + activity (both required)
python3 tracker.py --map my-project-dir PROJECT_ID ACTIVITY_ID
python3 tracker.py --map chrome:github.com PROJECT_ID ACTIVITY_ID
python3 tracker.py --map app:Slack PROJECT_ID ACTIVITY_ID
```

## Project mappings

Every logged activity is reduced to an **activity key** before it's mapped or aggregated:

| Source | Example window/tab | Activity key |
|---|---|---|
| Terminal | `my-project — zsh` | `my-project` (part before ` — `) |
| Chrome | any tab on `https://github.com/...` | `chrome:github.com` |
| Any other app | Slack, Figma, TextEdit, etc. | `app:Slack` |

```bash
# See your projects and activities and their IDs
python3 tracker.py --list-projects
python3 tracker.py --list-activities

# Associate an activity key with a Kimai project + activity
python3 tracker.py --map my-project-dir 42 7
```

Each mapping value is a `{"project": id, "activity": id}` pair, because Kimai timesheets require both. Mappings are saved in `project_mappings.json` (gitignored — stays local). Legacy Clockify mappings (a bare project id) still load — they're treated as project-only and fall back to `default_activity_id`. In practice you shouldn't need to map by hand much — see the next section.

## AI-powered project classification (optional)

Right before every send (midnight rollover, `--send-today`, `--send DATE`, or startup catch-up of missed days), the tracker looks at that day's entries for any activity key with no existing mapping, and asks Claude to classify all of them against your Kimai projects in **one batch API call** — using a few sample window/tab titles per key as context, not just the key string. Confident matches are saved to `project_mappings.json` and never re-queried; low-confidence keys are left unmapped and retried next time. A classification failure never blocks the send — entries that already have mappings still go through.

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
- type a name — fuzzy-matches against your existing projects; if nothing matches, offers to **create a new Kimai project** on the spot

### How it works

- Scans log files for the last N days (default 7, or the specific day being sent)
- Extracts activity keys and a few sample titles per key (same logic the tracker uses)
- Sends all unmapped keys + samples + all your Kimai project names to Claude in **one API call**
- Claude returns a JSON match for each key; conservative — prefers `null` over a weak guess
- Confirmed matches are written to `project_mappings.json`
- Keys where Claude returns no match remain unmapped and are retried next time
- Two guards keep it conservative: domains in `generic_domains` (sign-in flows, bare search homepages — see [Configuration reference](#configuration-reference)) are never even sent to Claude, and your own already-confirmed mappings are included as calibration examples so it has a concrete sense of what "specific enough" looks like in your data, not just the raw project list

## Migrating from Clockify

If you were running the old Clockify version, `migrate_clockify_to_kimai.py` mirrors your workspace into Kimai in one shot. Put **both** backends in `config.json` — the Kimai keys plus, temporarily, `clockify_api_key` and `clockify_workspace_id` — then:

```bash
python3 migrate_clockify_to_kimai.py --dry-run   # preview what it would create
python3 migrate_clockify_to_kimai.py             # do it
```

It:

- creates a Kimai **customer** for each Clockify client (client-less projects go under a `No client` customer),
- creates a Kimai **project** for each Clockify project, each with a `General` **activity** (Kimai needs an activity on every timesheet),
- creates a `Meetings` customer/project/activity for [calendar sync](#calendar-meeting-sync) and an `Internal / Misc` default,
- rewrites `project_mappings.json` from Clockify project IDs to Kimai `{project, activity}` pairs, and
- prints the `default_*` and `meetings_*` IDs to paste into `config.json`.

It's idempotent — existing Kimai customers/projects/activities are matched by name, so re-running won't duplicate anything. Once you've confirmed everything landed, you can drop the `clockify_*` keys from `config.json`.

## Calendar meeting sync

Turn this on to auto-log **external meetings** — any Google Calendar event that has at least one guest whose email isn't one of yours. Ad-hoc calls with no calendar event are still captured by the normal activity tracker; the two don't double-count (overlapping tracker time is trimmed away — see below).

### Setup

1. Create a Google Cloud project, enable the **Calendar API**, and create an OAuth **Desktop app** client ([console](https://console.cloud.google.com/apis/credentials)). Add these to `config.json`:
   ```json
   {
     "calendar_sync_enabled": true,
     "google_oauth_client_id": "...apps.googleusercontent.com",
     "google_oauth_client_secret": "...",
     "owner_emails": ["you@gmail.com", "you@work.com"],
     "meetings_project_id": 12,
     "meetings_activity_id": 34
   }
   ```
   - **owner_emails**: all your addresses. A guest in this set doesn't make a meeting "external".
   - **meetings_project_id / meetings_activity_id**: the fallback Kimai project+activity when Claude can't confidently classify a meeting (the migration script creates a `Meetings` project/activity for exactly this).
2. Authorize each Google account once (opens a browser):
   ```bash
   python3 calendar_sync.py --auth      # repeat per account
   python3 calendar_sync.py --list      # show authorized accounts
   ```
   Refresh tokens are stored in `google_tokens.json` (gitignored, chmod 600).
3. Preview what a day would sync, without writing anything:
   ```bash
   python3 calendar_sync.py --test 2026-07-09
   ```

That's it — no separate cron job. The sync runs from the tracker's normal send path (midnight rollover, `--send`, or startup catch-up), so it obeys the same "catch up whenever the Mac is next awake" behavior as the rest of the tracker.

### What qualifies

A calendar event is logged when **all** of these hold: it's a timed event (not all-day), not cancelled, you haven't declined it, and it has at least one guest whose email isn't in `owner_emails` (room/resource attendees don't count). The same meeting appearing on several of your calendars is de-duplicated by its iCal UID. Each meeting is classified to a Kimai project+activity by Claude (title + guest domains), falling back to `meetings_project_id/meetings_activity_id`. Already-synced meetings are tracked in `synced_meetings.json` so re-sends never duplicate.

### No double counting

The activity tracker keeps capturing Zoom/Meet time as usual (it's in `video_apps`/`video_domains`). At send time, any tracker time that overlaps a synced meeting block is **trimmed out** — the calendar sync owns the scheduled-meeting time, while tracker time that falls *outside* every meeting (e.g. an unscheduled call, or work before/after the meeting) is kept intact.

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

`check_clockify_mapping.py` is a Claude Code `UserPromptSubmit` hook. When you open a project in Claude Code that has no Kimai mapping yet, it prompts Claude to ask you which project to associate before proceeding.

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
├── calendar_sync.py            # Google Calendar → Kimai external-meeting sync (OAuth + filtering)
├── migrate_clockify_to_kimai.py # one-off: mirror a Clockify workspace into Kimai
├── common.py                   # shared config/Kimai/Anthropic/mapping helpers
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
