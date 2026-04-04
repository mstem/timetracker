#!/usr/bin/env python3
"""
Terminal window time tracker.
Polls Terminal.app every 2 seconds, logs time per window, and sends
aggregated daily entries to Clockify at midnight (or on next start).

Usage:
  python3 tracker.py                          # run as daemon
  python3 tracker.py --send-today             # manually send today's log
  python3 tracker.py --send DATE              # manually send a specific date (YYYY-MM-DD)
  python3 tracker.py --list-projects          # list all Clockify projects
  python3 tracker.py --map DIR PROJECT_ID     # map a project dir to a Clockify project ID
"""

import subprocess
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error
import signal
import logging
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
MAPPINGS_PATH = os.path.join(BASE_DIR, "project_mappings.json")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
SENT_FILE = os.path.join(BASE_DIR, "sent_dates.json")
LOG_FILE = os.path.join(BASE_DIR, "tracker.log")
POLL_INTERVAL = 2  # seconds
MIN_ENTRY_SECONDS = 5  # skip entries shorter than this when sending

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: config.json not found at {CONFIG_PATH}")
        print("Run setup.sh first and fill in your API key and workspace ID.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    for key in ("api_key", "workspace_id"):
        if not cfg.get(key) or cfg[key].startswith("<"):
            print(f"ERROR: '{key}' not set in config.json")
            sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Terminal window detection
# ---------------------------------------------------------------------------

APPLESCRIPT = '''
tell application "Terminal"
    if frontmost then
        return name of front window
    else
        return ""
    end if
end tell
'''

def get_active_window():
    """Return active Terminal.app window name, or None if Terminal isn't focused."""
    try:
        result = subprocess.run(
            ["osascript", "-e", APPLESCRIPT],
            capture_output=True, text=True, timeout=3
        )
        name = result.stdout.strip()
        return name if name else None
    except Exception as e:
        log.debug(f"osascript error: {e}")
        return None


# ---------------------------------------------------------------------------
# Log file helpers
# ---------------------------------------------------------------------------

def log_path_for(date: datetime.date) -> str:
    return os.path.join(LOGS_DIR, f"{date.isoformat()}.json")


def load_entries(path: str) -> list:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []


def append_entry(path: str, window: str, start: datetime.datetime, end: datetime.datetime):
    duration = int((end - start).total_seconds())
    if duration < 1:
        return
    entries = load_entries(path)
    entries.append({
        "window": window,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": duration,
    })
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


# ---------------------------------------------------------------------------
# Clockify
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Project mappings  (dir name → Clockify project ID)
# ---------------------------------------------------------------------------

def load_project_mappings() -> dict:
    if os.path.exists(MAPPINGS_PATH):
        with open(MAPPINGS_PATH) as f:
            return json.load(f)
    return {}


def save_project_mapping(dir_name: str, project_id: str):
    mappings = load_project_mappings()
    mappings[dir_name] = project_id
    with open(MAPPINGS_PATH, "w") as f:
        json.dump(mappings, f, indent=2)
    print(f"Mapped '{dir_name}' → {project_id}")


def window_dir(window_name: str) -> str:
    """Extract the leading directory segment from a Terminal window title."""
    return window_name.split(" — ")[0].strip()


# ---------------------------------------------------------------------------
# Clockify project listing
# ---------------------------------------------------------------------------

def list_clockify_projects(config: dict) -> list:
    """Return all non-archived projects in the workspace."""
    projects = clockify_request(
        "GET",
        f"/workspaces/{config['workspace_id']}/projects?archived=false&page-size=500",
        config["api_key"],
    )
    return projects


def cmd_list_projects():
    config = load_config()
    projects = list_clockify_projects(config)
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        client = f"  [{p.get('clientName', '')}]" if p.get("clientName") else ""
        print(f"  {p['id']}  {p['name']}{client}")


def aggregate_entries(entries: list) -> dict:
    """Group by window name → {window: {seconds, start, end}}."""
    groups = defaultdict(lambda: {"seconds": 0, "start": None, "end": None})
    for e in entries:
        w = e["window"]
        groups[w]["seconds"] += e["seconds"]
        if groups[w]["start"] is None or e["start"] < groups[w]["start"]:
            groups[w]["start"] = e["start"]
        if groups[w]["end"] is None or e["end"] > groups[w]["end"]:
            groups[w]["end"] = e["end"]
    return dict(groups)


def clockify_request(method: str, path: str, api_key: str, body: dict = None):
    url = f"https://api.clockify.me/api/v1{path}"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def send_to_clockify(config: dict, date_str: str, entries: list) -> int:
    """Send aggregated daily entries to Clockify. Returns number of entries sent."""
    groups = aggregate_entries(entries)
    mappings = load_project_mappings()
    sent = 0
    for window, data in groups.items():
        if data["seconds"] < MIN_ENTRY_SECONDS:
            log.info(f"Skipping '{window}' ({data['seconds']}s, under minimum)")
            continue

        start_dt = datetime.datetime.strptime(data["start"], "%Y-%m-%dT%H:%M:%SZ")
        end_dt = start_dt + datetime.timedelta(seconds=data["seconds"])

        # Per-project mapping takes priority; fall back to global project_id
        dir_name = window_dir(window)
        project_id = mappings.get(dir_name) or config.get("project_id") or None

        body = {
            "description": window,
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "billable": False,
        }
        if project_id:
            body["projectId"] = project_id

        try:
            clockify_request(
                "POST",
                f"/workspaces/{config['workspace_id']}/time-entries",
                config["api_key"],
                body,
            )
            mins = data["seconds"] // 60
            log.info(f"Sent [{date_str}] '{window}' → {mins}m")
            print(f"  ✓ '{window}' — {mins} min")
            sent += 1
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            log.error(f"Clockify HTTP {e.code} for '{window}': {err_body}")
            print(f"  ✗ '{window}' — HTTP {e.code}: {err_body}")
        except Exception as e:
            log.error(f"Clockify error for '{window}': {e}")
            print(f"  ✗ '{window}' — {e}")

    return sent


# ---------------------------------------------------------------------------
# Sent-dates tracking
# ---------------------------------------------------------------------------

def load_sent_dates() -> set:
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE) as f:
            return set(json.load(f))
    return set()


def mark_date_sent(date_str: str):
    sent = load_sent_dates()
    sent.add(date_str)
    with open(SENT_FILE, "w") as f:
        json.dump(sorted(sent), f)


# ---------------------------------------------------------------------------
# Manual send commands
# ---------------------------------------------------------------------------

def cmd_send(date_str: str):
    config = load_config()
    path = os.path.join(LOGS_DIR, f"{date_str}.json")
    entries = load_entries(path)
    if not entries:
        print(f"No log found for {date_str}")
        return
    print(f"Sending {len(entries)} raw entries for {date_str}...")
    sent = send_to_clockify(config, date_str, entries)
    if sent:
        mark_date_sent(date_str)
        print(f"Done — {sent} entries sent.")
    else:
        print("Nothing sent (all entries under 1 minute or errors).")


# ---------------------------------------------------------------------------
# Tracker daemon
# ---------------------------------------------------------------------------

class Tracker:
    def __init__(self):
        os.makedirs(LOGS_DIR, exist_ok=True)
        self.config = load_config()
        self.current_window: str | None = None
        self.window_start: datetime.datetime | None = None
        self.current_date = datetime.date.today()
        self.sent_dates = load_sent_dates()

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _flush_current(self, now: datetime.datetime):
        """Write the active window's elapsed time to the daily log."""
        if self.current_window and self.window_start:
            path = log_path_for(self.current_date)
            append_entry(path, self.current_window, self.window_start, now)
        self.current_window = None
        self.window_start = None

    def _check_midnight(self):
        today = datetime.date.today()
        if today == self.current_date:
            return
        # Day rolled over — send yesterday's log
        yesterday = self.current_date.isoformat()
        if yesterday not in self.sent_dates:
            log.info(f"Midnight rollover — sending {yesterday}")
            path = log_path_for(self.current_date)
            entries = load_entries(path)
            if entries:
                try:
                    send_to_clockify(self.config, yesterday, entries)
                    mark_date_sent(yesterday)
                    self.sent_dates.add(yesterday)
                except Exception as e:
                    log.error(f"Failed to send {yesterday}: {e}")
        self.current_date = today

    def _on_shutdown(self, signum, frame):
        log.info("Shutdown signal received — flushing current window.")
        self._flush_current(datetime.datetime.utcnow())
        sys.exit(0)

    def run(self):
        log.info("Tracker started.")
        # On startup, send any unsent past days
        self._send_missed_days()

        while True:
            now = datetime.datetime.utcnow()
            self._check_midnight()
            active = get_active_window()

            if active != self.current_window:
                self._flush_current(now)
                if active:
                    self.current_window = active
                    self.window_start = now
                    log.debug(f"Window focus: '{active}'")

            time.sleep(POLL_INTERVAL)

    def _send_missed_days(self):
        """Send any log files that were never sent (e.g. if daemon was off at midnight)."""
        today = datetime.date.today().isoformat()
        for fname in sorted(os.listdir(LOGS_DIR)):
            if not fname.endswith(".json"):
                continue
            date_str = fname[:-5]
            if date_str >= today:
                continue  # don't send today's partial log
            if date_str in self.sent_dates:
                continue
            log.info(f"Found unsent log for {date_str}, sending...")
            path = os.path.join(LOGS_DIR, fname)
            entries = load_entries(path)
            if entries:
                try:
                    send_to_clockify(self.config, date_str, entries)
                    mark_date_sent(date_str)
                    self.sent_dates.add(date_str)
                except Exception as e:
                    log.error(f"Failed to send missed day {date_str}: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--send-today":
            cmd_send(datetime.date.today().isoformat())
        elif sys.argv[1] == "--send" and len(sys.argv) == 3:
            cmd_send(sys.argv[2])
        elif sys.argv[1] == "--list-projects":
            cmd_list_projects()
        elif sys.argv[1] == "--map" and len(sys.argv) == 4:
            save_project_mapping(sys.argv[2], sys.argv[3])
        else:
            print(__doc__)
    else:
        Tracker().run()
