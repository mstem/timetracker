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
  python3 tracker.py --sync-android [DATE]         # pull the phone's day from RescueTime, don't send
  python3 tracker.py --send-android DATE           # send only the phone batch (when the Mac side already went)
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
MISSED_RETRY_INTERVAL = 3600  # seconds between re-sweeps for days that failed to send
SLEEP_GAP_SECONDS = 60  # a gap this long between 2s polls means the process wasn't running

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

# iTerm2 exposes the session's working directory, which Terminal.app does not.
# Without this every iTerm2 session collapsed into a single `app:iterm2` key,
# because Claude Code replaces the window title with its own status line and
# there is no directory left in the text to parse.
ITERM_PATH_SCRIPT = '''
tell application "iTerm2"
    if (count of windows) > 0 then
        tell current session of current window
            return variable named "path"
        end tell
    else
        return ""
    end if
end tell
'''

# Terminal.app keys off the window title; iTerm2 keys off the reported cwd.
TERMINAL_APPS = {"Terminal", "iTerm2"}


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


def get_iterm_path():
    """Return the frontmost iTerm2 session's working directory, or None."""
    try:
        result = subprocess.run(
            ["osascript", "-e", ITERM_PATH_SCRIPT],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode != 0:
            log.debug(f"osascript iterm path exit {result.returncode}: {result.stderr.strip()}")
            return None
        return result.stdout.strip() or None
    except Exception as e:
        log.debug(f"osascript iterm path error: {e}")
        return None


def classify_frontmost(config: dict):
    """Return the current ActivityState, or None if nothing trackable is frontmost."""
    fm = get_frontmost()
    if fm is None:
        return None
    app_name, window_title = fm

    if app_name == "Firefox":
        return None  # out of scope for now

    path = None
    if app_name in TERMINAL_APPS:
        source, url = "terminal", None
        if app_name == "iTerm2":
            path = get_iterm_path()
            if not path:
                # No cwd means no reliable key. Falling back to the window title
                # would file Claude Code status text as a directory name, so treat
                # it as a plain app instead and let it show up as app:iterm2.
                source = "app"
    elif app_name == "Google Chrome":
        source = "chrome"
        url = get_chrome_url()
        if window_title.endswith(CHROME_TITLE_SUFFIX):
            window_title = window_title[: -len(CHROME_TITLE_SUFFIX)]
    else:
        source, url = "app", None

    if common.is_blocked(source, app_name, url, config):
        return None

    key = common.activity_key(source, window_title, url=url, app=app_name, path=path)
    return ActivityState(source=source, app=app_name, window=window_title, url=url, key=key)


# ---------------------------------------------------------------------------
# Log file helpers
# ---------------------------------------------------------------------------

def log_path_for(date: datetime.date) -> str:
    return os.path.join(LOGS_DIR, f"{date.isoformat()}.json")


def android_dates() -> set:
    """Dates with a stored phone log. Note this only finds days already synced:
    a day the Mac was off entirely leaves no local log to discover, so use
    `--send DATE` by hand for those."""
    d = common.ANDROID_LOGS_DIR
    if not os.path.isdir(d):
        return set()
    return {f[:-5] for f in os.listdir(d) if f.endswith(".json")}


def has_android_data(date_str: str) -> bool:
    return os.path.exists(os.path.join(common.ANDROID_LOGS_DIR, f"{date_str}.json"))


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


def subtract_intervals(entries: list, intervals: list) -> list:
    """Clip each entry to the sub-spans falling *outside* every given interval,
    dropping whatever is fully covered. `intervals` is a list of
    (start_utc, end_utc) naive datetimes.

    Implements the "suppress overlaps only" dedup decision, used for both
    calendar meetings and phone time that overlaps Mac time.
    """
    if not intervals:
        return entries
    result = []
    for e in entries:
        spans = [(_parse_z(e["start"]), _parse_z(e["end"]))]
        for m_start, m_end in intervals:
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


def subtract_meetings(entries: list, meetings: list) -> list:
    """Remove time already logged by the calendar sync, so meeting time isn't
    counted twice. Ad-hoc call time with no calendar event survives untouched."""
    return subtract_intervals(entries, meetings)


def entry_intervals(entries: list) -> list:
    """The (start, end) UTC intervals covered by a set of log entries."""
    return [(_parse_z(e["start"]), _parse_z(e["end"])) for e in entries]


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


def send_to_kimai(config: dict, date_str: str, entries: list) -> tuple:
    """Sync external calendar meetings, then classify newly-seen activity and
    send aggregated daily tracker entries to Kimai (minus any time already
    covered by a synced meeting).

    Returns (sent, failed). Per-entry errors are caught so one bad group can't
    abort the rest, so callers must check `failed` before marking the day sent —
    an exception never reaches them."""
    meetings = _sync_meetings(config, date_str)
    entries = subtract_meetings(entries, meetings)

    mappings = ai_matcher.classify_and_save(config, entries)
    default_project = config.get("default_project_id")
    default_activity = config.get("default_activity_id")

    def resolve_mac(key: str):
        mapping = mappings.get(key) or {}
        return (mapping.get("project") or default_project,
                mapping.get("activity") or default_activity)

    sent, failed = _post_groups(config, date_str, aggregate_entries(entries), resolve_mac)
    a_sent, a_failed = _send_android(config, date_str, entries, meetings, mappings)
    return sent + a_sent, failed + a_failed


def _send_android(config: dict, date_str: str, mac_entries: list, meetings: list,
                  mappings: dict) -> tuple:
    """Pull the phone's app usage for the day and send it to Kimai as its own
    tagged batch.

    Phone spans are clipped against Mac spans and meeting spans first: without
    that, a glance at the phone mid-coding bills twice and a day's total stops
    meaning wall-clock time. Phone time while the laptop was idle survives whole.

    Routing order per app: an explicit per-app mapping wins, then the RescueTime
    category table, then the configured defaults. No LLM call happens here — the
    category table already routes every app, and an app name with no title or URL
    gives a classifier nothing to work with.
    """
    if not config.get("android_sync_enabled"):
        return 0, 0
    try:
        import android_sync
    except Exception as e:
        log.error(f"android_sync import failed: {e}")
        return 0, 0

    phone = android_sync.sync_day(config, date_str)
    if not phone:
        return 0, 0

    before = sum(e["seconds"] for e in phone)
    phone = subtract_intervals(phone, entry_intervals(mac_entries) + list(meetings))
    after = sum(e["seconds"] for e in phone)
    if before != after:
        log.info(f"Android: clipped {(before - after) // 60}m of {before // 60}m "
                 f"that overlapped Mac or meeting time")
    if not phone:
        log.info("Android: everything overlapped Mac time, nothing to send")
        return 0, 0

    # An app has one RescueTime category, but pick the longest-running one per
    # key rather than the first in case a name maps to two.
    cat_seconds = defaultdict(lambda: defaultdict(int))
    for e in phone:
        cat_seconds[common.entry_key(e)][e.get("category") or ""] += e["seconds"]
    cat_by_key = {k: max(v.items(), key=lambda kv: kv[1])[0] for k, v in cat_seconds.items()}

    categories = android_sync.load_categories()
    default_project = config.get("default_project_id")
    default_activity = config.get("default_activity_id")

    def resolve_android(key: str):
        mapping = mappings.get(key) or {}
        if mapping.get("project"):
            return mapping["project"], mapping.get("activity") or default_activity
        target = categories.get(cat_by_key.get(key, "")) or {}
        if target.get("project"):
            return target["project"], target.get("activity") or default_activity
        return default_project, default_activity

    common.ensure_kimai_tag(config, "android")
    return _post_groups(config, date_str, aggregate_entries(phone), resolve_android,
                        tags="android")


def _post_groups(config: dict, date_str: str, groups: dict, resolve, tags: str = None) -> tuple:
    """POST one aggregated group per Kimai timesheet. `resolve(key)` returns the
    (project_id, activity_id) that group belongs to.

    Returns (sent, failed). Per-entry errors are caught so one bad group can't
    abort the rest.
    """
    sent = 0
    failed = 0
    for key, data in groups.items():
        if data["seconds"] < MIN_ENTRY_SECONDS:
            log.info(f"Skipping '{key}' ({data['seconds']}s, under minimum)")
            continue

        start_dt = _parse_z(data["start"])
        end_dt = start_dt + datetime.timedelta(seconds=data["seconds"])
        description = describe_group(key, data)

        project_id, activity_id = resolve(key)
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
        if tags:
            body["tags"] = tags

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
            failed += 1
        except Exception as e:
            log.error(f"Kimai error for '{key}': {e}")
            print(f"  ✗ '{description}' — {e}")
            failed += 1

    return sent, failed


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
    if not entries and not (config.get("android_sync_enabled") or has_android_data(date_str)):
        print(f"No log found for {date_str}")
        return
    print(f"Sending {len(entries)} raw Mac entries for {date_str}...")
    sent, failed = send_to_kimai(config, date_str, entries)
    if failed:
        print(f"Done — {sent} entries sent, {failed} failed. "
              f"{date_str} left unsent so it retries later.")
    else:
        mark_date_sent(date_str)
        print(f"Done — {sent} entries sent.")


def cmd_sync_android(date_str: str):
    """Pull the phone's day from RescueTime and store it, without sending."""
    import android_sync
    config = common.load_config()
    entries = android_sync.sync_day(config, date_str)
    if not entries:
        print(f"No Android activity for {date_str}.")
        return
    groups = aggregate_entries(entries)
    print(f"{date_str}: {len(entries)} phone entries, "
          f"{sum(e['seconds'] for e in entries) / 3600:.2f}h across {len(groups)} apps")
    for key, data in sorted(groups.items(), key=lambda kv: -kv[1]["seconds"]):
        print(f"  {data['seconds'] / 60:6.1f}m  {key}")


def cmd_send_android(date_str: str):
    """Send only the phone batch for a day. Use this when the Mac side of that
    day is already in Kimai — a plain --send would duplicate it."""
    config = common.load_config()
    mac = load_entries(os.path.join(LOGS_DIR, f"{date_str}.json"))
    mappings = common.load_project_mappings()
    print(f"Sending phone time for {date_str} "
          f"(clipping against {len(mac)} Mac entries already in Kimai)...")
    sent, failed = _send_android(config, date_str, mac, [], mappings)
    print(f"Done — {sent} phone entries sent, {failed} failed.")


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
        self._idle_probe_failing: bool = False
        self.current_date = datetime.date.today()
        self.sent_dates = load_sent_dates()

        self._kimai_active_cache: bool = False
        self._kimai_last_check: datetime.datetime = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=KIMAI_CHECK_INTERVAL)
        )

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _android_hold(self, date_str: str) -> bool:
        """True while a finished day should still wait for RescueTime's next
        upload before being sent. Local time throughout, matching how log files
        are named."""
        if not self.config.get("android_sync_enabled"):
            return False
        try:
            delay = int(self.config.get("android_sync_delay_minutes", 45))
            day = datetime.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            return False
        ready_at = (datetime.datetime.combine(day, datetime.time.min)
                    + datetime.timedelta(days=1, minutes=delay))
        return datetime.datetime.now() < ready_at

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

    def _idle_close_point(self, now: datetime.datetime, idle_seconds: float,
                          in_video: bool) -> datetime.datetime:
        """When to end a session that just went idle.

        Normally that's the moment input actually stopped, not the moment idle
        crossed the threshold — the gap in between isn't work. Video is the
        exception: silence is how watching looks, so the whole stretch counts
        and the threshold acts purely as a ceiling.
        """
        idle_since = now if in_video else now - datetime.timedelta(seconds=idle_seconds)
        if self.activity_start and idle_since < self.activity_start:
            idle_since = self.activity_start
        return idle_since

    def _idle_probe_warn(self):
        """Log the first idle-probe failure, then stay quiet — the loop polls
        every POLL_INTERVAL seconds and would otherwise flood the log."""
        if not self._idle_probe_failing:
            log.warning("Idle probe (ioreg) unreadable — capture paused until it recovers")
            self._idle_probe_failing = True

    def _idle_probe_ok(self):
        if self._idle_probe_failing:
            log.info("Idle probe recovered — capture resumed")
            self._idle_probe_failing = False

    def _gap_check(self, now: datetime.datetime, last_tick: datetime.datetime):
        """Close out the current session if the clock jumped since the last poll.

        The loop ticks every POLL_INTERVAL seconds, so a gap of minutes means the
        process wasn't running — almost always because the machine slept. Idle
        detection can't catch this: HIDIdleTime resets when a keypress wakes the
        machine, so on the next tick the user looks active and the frontmost app
        is unchanged, and the whole sleep window gets billed to that app. Bill
        only up to the last tick we actually observed.
        """
        gap = (now - last_tick).total_seconds()
        if gap < SLEEP_GAP_SECONDS:
            return
        if self.current_activity:
            log.info(
                f"Clock jumped {gap / 60:.0f}m (machine asleep?) — closed "
                f"'{self.current_activity.key}' at {last_tick.isoformat()}"
            )
            self._flush_current(last_tick)
        else:
            log.info(f"Clock jumped {gap / 60:.0f}m (machine asleep?)")
        # Treat the wake as a fresh start rather than a continuation.
        self.is_idle = True

    def _check_midnight(self):
        today = datetime.date.today()
        if today == self.current_date:
            return
        # Day rolled over — send yesterday's log
        yesterday = self.current_date.isoformat()
        if yesterday not in self.sent_dates and self._android_hold(yesterday):
            # RescueTime's free tier uploads on a 30-minute cycle, so sending at
            # 00:00 would miss the last stretch of phone use, and sent_dates has
            # no per-source dimension to let it be added later. Let the hourly
            # _send_missed_days sweep pick the day up once the data has landed.
            log.info(f"Midnight rollover — holding {yesterday} for the phone upload lag")
            self.current_date = today
            return
        if yesterday not in self.sent_dates:
            log.info(f"Midnight rollover — sending {yesterday}")
            path = log_path_for(self.current_date)
            entries = load_entries(path)
            if entries:
                try:
                    sent, failed = send_to_kimai(self.config, yesterday, entries)
                    self._settle_send(yesterday, sent, failed)
                except Exception as e:
                    log.error(f"Failed to send {yesterday}: {e}")
        # Always advance the date, even if the send failed — otherwise the
        # rollover re-fires on every poll. Unsent days are retried by
        # _send_missed_days instead.
        self.current_date = today

    def _on_shutdown(self, signum, frame):
        log.info("Shutdown signal received — flushing current activity.")
        now = datetime.datetime.utcnow()
        self._flush_current(now)
        sys.exit(0)

    def run(self):
        log.info("Tracker started.")
        common.check_kimai_timezone(self.config)
        # On startup, send any unsent past days
        self._send_missed_days()
        last_retry = datetime.datetime.utcnow()
        last_tick = datetime.datetime.utcnow()

        while True:
            now = datetime.datetime.utcnow()
            self._gap_check(now, last_tick)
            last_tick = now
            self._check_midnight()

            # The midnight send often lands before the network is back after a
            # wake. Re-sweep periodically so a failed day doesn't sit unsent
            # until the next daemon restart.
            if (now - last_retry).total_seconds() >= MISSED_RETRY_INTERVAL:
                last_retry = now
                self._send_missed_days()
            timer_running = self._has_kimai_active_timer()

            if timer_running:
                # A Kimai timesheet is running — discard any in-progress tracking
                if self.current_activity:
                    self._discard_current()
                time.sleep(POLL_INTERVAL)
                continue

            idle_threshold = self.config.get("idle_threshold_seconds", common.DEFAULT_IDLE_THRESHOLD_SECONDS)
            idle_seconds = common.get_idle_seconds()
            if idle_seconds is None:
                # Idle state unknown — bank what we have rather than guess the
                # user is still here. Mirrors the get_frontmost probe pause.
                if self.current_activity:
                    self._idle_probe_warn()
                    self._flush_current(now)
                time.sleep(POLL_INTERVAL)
                continue
            self._idle_probe_ok()

            in_video = self.current_activity is not None and common.is_video_context(
                self.current_activity.source, self.current_activity.app,
                self.current_activity.url, self.config,
            )

            if in_video:
                # Video gets a much longer leash than normal input-driven work,
                # but not an unlimited one — see DEFAULT_VIDEO_IDLE_THRESHOLD_SECONDS.
                idle_threshold = self.config.get(
                    "video_idle_threshold_seconds",
                    common.DEFAULT_VIDEO_IDLE_THRESHOLD_SECONDS,
                )

            if idle_seconds >= idle_threshold:
                if not self.is_idle:
                    idle_since = self._idle_close_point(now, idle_seconds, in_video)
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
        dates = {f[:-5] for f in os.listdir(LOGS_DIR) if f.endswith(".json")}
        dates |= android_dates()
        for date_str in sorted(dates):
            if date_str >= today:
                continue  # don't send today's partial log
            if date_str in self.sent_dates:
                continue
            if self._android_hold(date_str):
                continue
            log.info(f"Found unsent log for {date_str}, sending...")
            entries = load_entries(os.path.join(LOGS_DIR, f"{date_str}.json"))
            if entries or self.config.get("android_sync_enabled"):
                try:
                    sent, failed = send_to_kimai(self.config, date_str, entries)
                    self._settle_send(date_str, sent, failed)
                except Exception as e:
                    log.error(f"Failed to send missed day {date_str}: {e}")

    def _settle_send(self, date_str: str, sent: int, failed: int) -> bool:
        """Decide whether a send counts as done. Returns True if the day was
        marked sent.

        A day is only left unsent when *nothing* got through — the network-down
        case, which is safe to retry wholesale. A partial failure is marked sent
        anyway: re-sending would duplicate the entries that did land, so the
        stragglers are logged for manual entry instead."""
        if failed and not sent:
            log.warning(f"{date_str}: all {failed} entries failed to send — will retry")
            return False
        if failed:
            log.error(
                f"{date_str}: {sent} sent but {failed} failed. Marking sent to avoid "
                f"duplicating the {sent} that landed — add the failures manually "
                f"(see the 'Kimai' errors above)."
            )
        mark_date_sent(date_str)
        self.sent_dates.add(date_str)
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--send-today":
            cmd_send(datetime.date.today().isoformat())
        elif sys.argv[1] == "--send" and len(sys.argv) == 3:
            cmd_send(sys.argv[2])
        elif sys.argv[1] == "--sync-android":
            cmd_sync_android(sys.argv[2] if len(sys.argv) == 3 else datetime.date.today().isoformat())
        elif sys.argv[1] == "--send-android" and len(sys.argv) == 3:
            cmd_send_android(sys.argv[2])
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
