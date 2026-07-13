#!/usr/bin/env python3
"""
Mac activity time tracker.
Polls the frontmost app/window every 2 seconds — Terminal, Chrome tabs (with
URL), or any other app via System Events — logs time per activity, and sends
aggregated daily entries to Kimai at midnight (or on next start). Any
newly-seen activity is classified into a Kimai project by Claude right
before sending. External calendar meetings are synced to Kimai on the same
schedule (see calendar_sync).

Usage:
  python3 tracker.py                               # run as daemon
  python3 tracker.py --send-today                  # manually send today's log
  python3 tracker.py --send DATE                   # manually send a specific date (YYYY-MM-DD)
  python3 tracker.py --list-projects               # list all Kimai projects
  python3 tracker.py --list-activities             # list all Kimai activities
  python3 tracker.py --map KEY PROJECT_ID ACTIVITY_ID  # map an activity key to a Kimai project+activity
"""

from __future__ import annotations

import subprocess
import json
import os
import sys
import time
import datetime
import urllib.error
import signal
import logging
from collections import defaultdict
from dataclasses import dataclass

import common
import ai_matcher

BASE_DIR = common.BASE_DIR
LOGS_DIR = common.LOGS_DIR
SENT_FILE = os.path.join(BASE_DIR, "sent_dates.json")
LOG_FILE = os.path.join(BASE_DIR, "tracker.log")
POLL_INTERVAL = 2  # seconds
MIN_ENTRY_SECONDS = 5  # hardcoded final floor when aggregating for send — not a tuning knob
KIMAI_CHECK_INTERVAL = 60  # seconds between Kimai active-timesheet API checks

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Activity capture
# ---------------------------------------------------------------------------

SYSTEM_EVENTS_SCRIPT = '''
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    try
        set winName to name of front window of frontApp
    on error
        set winName to ""
    end try
end tell
return appName & (ASCII character 31) & winName
'''

CHROME_URL_SCRIPT = '''
tell application "Google Chrome"
    if (count of windows) > 0 then
        return URL of active tab of front window
    else
        return ""
    end if
end tell
'''

CHROME_TITLE_SUFFIX = " - Google Chrome"


@dataclass
class ActivityState:
    source: str   # "terminal" | "chrome" | "app"
    app: str
    window: str
    url: str | None
    key: str


# A failed probe (osascript nonzero exit, timeout, or a wedged System Events
# returning -600) looks identical to "nothing trackable is frontmost": both
# yield no activity. But a persistent failure means capture is silently paused,
# so surface it at WARN — throttled to ~once/min so a stuck System Events
# doesn't flood the log at the 2s poll rate — and note when it recovers.
_PROBE_WARN_INTERVAL = 60  # seconds
_last_probe_warn = 0.0
_probe_failing = False


def _warn_probe_failure(detail: str):
    global _last_probe_warn, _probe_failing
    _probe_failing = True
    now = time.monotonic()
    if now - _last_probe_warn >= _PROBE_WARN_INTERVAL:
        log.warning(f"get_frontmost probe failed ({detail}) — capture paused until it recovers")
        _last_probe_warn = now


def _note_probe_ok():
    global _probe_failing
    if _probe_failing:
        log.info("get_frontmost probe recovered — capture resumed")
        _probe_failing = False


def get_frontmost():
    """Return (app_name, window_title) for whichever app is frontmost, or None.

    Returns None both when nothing trackable is frontmost and when the probe
    itself fails; the latter is logged (see _warn_probe_failure) rather than
    silently swallowed.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", SYSTEM_EVENTS_SCRIPT],
            capture_output=True, text=True, timeout=3
        )
    except Exception as e:
        _warn_probe_failure(f"osascript did not run: {e}")
        return None
    if result.returncode != 0:
        _warn_probe_failure(f"osascript exit {result.returncode}: {result.stderr.strip()}")
        return None
    _note_probe_ok()
    raw = result.stdout.rstrip("\r\n")
    if "\x1f" not in raw:
        return None
    app_name, window_title = raw.split("\x1f", 1)
    return (app_name, window_title) if app_name else None


def get_chrome_url():
    """Return the active Chrome tab's URL, or None."""
    try:
        result = subprocess.run(
            ["osascript", "-e", CHROME_URL_SCRIPT],
            capture_output=True, text=True, timeout=3
        )
        url = result.stdout.strip()
        return common.strip_url_params(url) or None
    except Exception as e:
        log.debug(f"osascript chrome url error: {e}")
        return None


def classify_frontmost(config: dict):
    """Return the current ActivityState, or None if nothing trackable is frontmost."""
    fm = get_frontmost()
    if fm is None:
        return None
    app_name, window_title = fm

    if app_name == "Firefox":
        return None  # out of scope for now

    if app_name == "Terminal":
        source, url = "terminal", None
    elif app_name == "Google Chrome":
        source = "chrome"
        url = get_chrome_url()
        if window_title.endswith(CHROME_TITLE_SUFFIX):
            window_title = window_title[: -len(CHROME_TITLE_SUFFIX)]
    else:
        source, url = "app", None

    if common.is_blocked(source, app_name, url, config):
        return None

    key = common.activity_key(source, window_title, url=url, app=app_name)
    return ActivityState(source=source, app=app_name, window=window_title, url=url, key=key)


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


def append_activity(path: str, activity: ActivityState, start: datetime.datetime,
                     end: datetime.datetime, min_duration: int):
    duration = int((end - start).total_seconds())
    if duration < min_duration:
        return
    entries = load_entries(path)
    entries.append({
        "source": activity.source,
        "app": activity.app,
        "window": activity.window,
        "url": activity.url,
        "key": activity.key,
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seconds": duration,
    })
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


# ---------------------------------------------------------------------------
# Project mappings  (--map CLI convenience wrapper around common.py)
# ---------------------------------------------------------------------------

def cmd_map(key: str, project_id: str, activity_id: str):
    key = common.normalize_key(key)
    mappings = common.load_project_mappings()
    mappings[key] = {"project": int(project_id), "activity": int(activity_id)}
    common.save_project_mappings(mappings)
    print(f"Mapped '{key}' → project {project_id}, activity {activity_id}")


def cmd_list_projects():
    config = common.load_config()
    projects = common.list_kimai_projects(config)
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        customer = p.get("customer")
        cust = ""
        if isinstance(customer, dict) and customer.get("name"):
            cust = f"  [{customer['name']}]"
        print(f"  {p['id']}  {p['name']}{cust}")


def cmd_list_activities():
    config = common.load_config()
    activities = common.list_kimai_activities(config)
    if not activities:
        print("No activities found.")
        return
    for a in activities:
        proj = a.get("project")
        scope = f"  (project {proj})" if proj else "  (global)"
        print(f"  {a['id']}  {a['name']}{scope}")


# ---------------------------------------------------------------------------
# Aggregation + sending
# ---------------------------------------------------------------------------

def aggregate_entries(entries: list) -> dict:
    """Group by activity key → {key: {seconds, start, end, titles: {title: seconds}}}."""
    groups = defaultdict(lambda: {"seconds": 0, "start": None, "end": None, "titles": defaultdict(int)})
    for e in entries:
        key = common.entry_key(e)
        g = groups[key]
        g["seconds"] += e["seconds"]
        title = e.get("window", key)
        g["titles"][title] += e["seconds"]
        if g["start"] is None or e["start"] < g["start"]:
            g["start"] = e["start"]
        if g["end"] is None or e["end"] > g["end"]:
            g["end"] = e["end"]
    return dict(groups)


def describe_group(key: str, group: dict) -> str:
    """Pick the raw title with the greatest total duration in the group as the
    Kimai description — resists title churn better than picking by count."""
    if not group["titles"]:
        return key
    return max(group["titles"].items(), key=lambda kv: kv[1])[0]


def _parse_z(ts: str) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


def subtract_meetings(entries: list, meetings: list) -> list:
    """Remove the time spans covered by synced calendar meetings from the raw
    tracker entries, so meeting time isn't counted twice (the calendar sync
    already logged it). Each entry is clipped to the sub-spans that fall
    *outside* every meeting interval; ad-hoc call time with no calendar event
    survives untouched. `meetings` is a list of (start_utc, end_utc) datetimes.

    Implements the "suppress overlaps only" dedup decision.
    """
    if not meetings:
        return entries
    result = []
    for e in entries:
        spans = [(_parse_z(e["start"]), _parse_z(e["end"]))]
        for m_start, m_end in meetings:
            next_spans = []
            for s, x in spans:
                if m_end <= s or m_start >= x:      # no overlap
                    next_spans.append((s, x))
                    continue
                if s < m_start:                     # keep the piece before the meeting
                    next_spans.append((s, m_start))
                if m_end < x:                       # keep the piece after the meeting
                    next_spans.append((m_end, x))
            spans = next_spans
        for s, x in spans:
            secs = int((x - s).total_seconds())
            if secs <= 0:
                continue
            clipped = dict(e)
            clipped["start"] = s.strftime("%Y-%m-%dT%H:%M:%SZ")
            clipped["end"] = x.strftime("%Y-%m-%dT%H:%M:%SZ")
            clipped["seconds"] = secs
            result.append(clipped)
    return result


def _sync_meetings(config: dict, date_str: str) -> list:
    """Log qualifying external calendar meetings to Kimai and return their UTC
    (start, end) intervals for overlap suppression. No-op unless calendar sync
    is enabled and configured. Never blocks the tracker send on failure."""
    if not config.get("calendar_sync_enabled"):
        return []
    try:
        import calendar_sync
    except Exception as e:
        log.error(f"calendar_sync import failed: {e}")
        return []
    try:
        return calendar_sync.sync_day(config, date_str)
    except Exception as e:
        log.error(f"Calendar sync failed for {date_str}: {e}")
        return []


def send_to_kimai(config: dict, date_str: str, entries: list) -> int:
    """Sync external calendar meetings, then classify newly-seen activity and
    send aggregated daily tracker entries to Kimai (minus any time already
    covered by a synced meeting)."""
    meetings = _sync_meetings(config, date_str)
    entries = subtract_meetings(entries, meetings)

    mappings = ai_matcher.classify_and_save(config, entries)
    groups = aggregate_entries(entries)
    default_project = config.get("default_project_id")
    default_activity = config.get("default_activity_id")
    sent = 0
    for key, data in groups.items():
        if data["seconds"] < MIN_ENTRY_SECONDS:
            log.info(f"Skipping '{key}' ({data['seconds']}s, under minimum)")
            continue

        start_dt = _parse_z(data["start"])
        end_dt = start_dt + datetime.timedelta(seconds=data["seconds"])
        description = describe_group(key, data)

        mapping = mappings.get(key) or {}
        project_id = mapping.get("project") or default_project
        activity_id = mapping.get("activity") or default_activity
        if not project_id or not activity_id:
            # Kimai requires both — without a mapping and without configured
            # defaults there's nowhere to file this; leave it for a later run.
            log.warning(f"Skipping '{key}' ({data['seconds']}s): no project/activity mapping and no defaults set")
            print(f"  ⚠ '{description}' — no Kimai project/activity (mapping or defaults missing)")
            continue

        body = {
            "begin": common.kimai_local_time(start_dt, config),
            "end": common.kimai_local_time(end_dt, config),
            "project": project_id,
            "activity": activity_id,
            "description": description,
            "billable": False,
            "exported": False,
        }

        try:
            common.kimai_request(config, "POST", "/timesheets", body)
            mins = data["seconds"] // 60
            log.info(f"Sent [{date_str}] '{description}' ({key}) → {mins}m")
            print(f"  ✓ '{description}' — {mins} min")
            sent += 1
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            log.error(f"Kimai HTTP {e.code} for '{key}': {err_body}")
            print(f"  ✗ '{description}' — HTTP {e.code}: {err_body}")
        except Exception as e:
            log.error(f"Kimai error for '{key}': {e}")
            print(f"  ✗ '{description}' — {e}")

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
    config = common.load_config()
    path = os.path.join(LOGS_DIR, f"{date_str}.json")
    entries = load_entries(path)
    if not entries:
        print(f"No log found for {date_str}")
        return
    print(f"Sending {len(entries)} raw entries for {date_str}...")
    sent = send_to_kimai(config, date_str, entries)
    if sent:
        mark_date_sent(date_str)
        print(f"Done — {sent} entries sent.")
    else:
        print("Nothing sent (all entries under the minimum duration or errors).")


# ---------------------------------------------------------------------------
# Tracker daemon
# ---------------------------------------------------------------------------

class Tracker:
    def __init__(self):
        os.makedirs(LOGS_DIR, mode=0o700, exist_ok=True)
        os.chmod(LOGS_DIR, 0o700)  # makedirs mode is ignored when the dir already exists
        self.config = common.load_config()
        self.current_activity: ActivityState | None = None
        self.activity_start: datetime.datetime | None = None
        self.is_idle: bool = False
        self.current_date = datetime.date.today()
        self.sent_dates = load_sent_dates()

        self._kimai_active_cache: bool = False
        self._kimai_last_check: datetime.datetime = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=KIMAI_CHECK_INTERVAL)
        )

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _min_duration_for(self, source: str) -> int:
        configured = self.config.get("min_duration_seconds", {})
        return configured.get(source, common.DEFAULT_MIN_DURATION_SECONDS.get(source, 1))

    def _flush_current(self, now: datetime.datetime):
        """Write the current activity's elapsed time to the daily log."""
        if self.current_activity and self.activity_start and now > self.activity_start:
            path = log_path_for(self.current_date)
            min_duration = self._min_duration_for(self.current_activity.source)
            append_activity(path, self.current_activity, self.activity_start, now, min_duration)
        self.current_activity = None
        self.activity_start = None

    def _discard_current(self):
        """Discard the current activity without logging it."""
        self.current_activity = None
        self.activity_start = None

    def _has_kimai_active_timer(self) -> bool:
        """Return True if Kimai has a running timesheet. Result is cached for KIMAI_CHECK_INTERVAL seconds."""
        now = datetime.datetime.utcnow()
        if (now - self._kimai_last_check).total_seconds() < KIMAI_CHECK_INTERVAL:
            return self._kimai_active_cache
        try:
            # /timesheets/active is already scoped to the authenticated user.
            self._kimai_active_cache = bool(common.kimai_active_timesheet(self.config))
        except Exception as e:
            log.debug(f"Kimai active-timer check failed: {e}")
            self._kimai_active_cache = False
        self._kimai_last_check = now
        return self._kimai_active_cache

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
                    send_to_kimai(self.config, yesterday, entries)
                    mark_date_sent(yesterday)
                    self.sent_dates.add(yesterday)
                except Exception as e:
                    log.error(f"Failed to send {yesterday}: {e}")
        self.current_date = today

    def _on_shutdown(self, signum, frame):
        log.info("Shutdown signal received — flushing current activity.")
        now = datetime.datetime.utcnow()
        self._flush_current(now)
        sys.exit(0)

    def run(self):
        log.info("Tracker started.")
        # On startup, send any unsent past days
        self._send_missed_days()

        while True:
            now = datetime.datetime.utcnow()
            self._check_midnight()
            timer_running = self._has_kimai_active_timer()

            if timer_running:
                # A Kimai timesheet is running — discard any in-progress tracking
                if self.current_activity:
                    self._discard_current()
                time.sleep(POLL_INTERVAL)
                continue

            idle_threshold = self.config.get("idle_threshold_seconds", common.DEFAULT_IDLE_THRESHOLD_SECONDS)
            idle_seconds = common.get_idle_seconds()
            in_video = self.current_activity is not None and common.is_video_context(
                self.current_activity.source, self.current_activity.app,
                self.current_activity.url, self.config,
            )

            if idle_seconds >= idle_threshold and not in_video:
                if not self.is_idle:
                    # Close out the session at the moment input actually stopped,
                    # not at the moment idle crossed the threshold.
                    idle_since = now - datetime.timedelta(seconds=idle_seconds)
                    if self.activity_start and idle_since < self.activity_start:
                        idle_since = self.activity_start
                    self._flush_current(idle_since)
                    self.is_idle = True
                    log.info(f"Idle for {idle_seconds:.0f}s — closed session at {idle_since.isoformat()}")
                time.sleep(POLL_INTERVAL)
                continue

            if self.is_idle:
                log.info("No longer idle — resuming capture.")
                self.is_idle = False

            activity = classify_frontmost(self.config)
            current_key = self.current_activity.key if self.current_activity else None
            new_key = activity.key if activity else None

            if new_key != current_key:
                self._flush_current(now)
                if activity:
                    self.current_activity = activity
                    self.activity_start = now
                    log.debug(f"Activity focus: '{activity.key}' ({activity.window})")

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
                    send_to_kimai(self.config, date_str, entries)
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
        elif sys.argv[1] == "--list-activities":
            cmd_list_activities()
        elif sys.argv[1] == "--map" and len(sys.argv) == 5:
            cmd_map(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            print(__doc__)
    else:
        Tracker().run()
