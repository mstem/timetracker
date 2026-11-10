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
  python3 tracker.py --self-test                   # check the send-state logic, no network
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
import shutil
import signal
import logging
import re
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

# The tab's own title as well as its URL. A window the user has named
# ("EVENS", "personal") shows that name in its title bar instead of the tab's,
# so without this every tab in a named window looked the same.
CHROME_URL_SCRIPT = '''
tell application "Google Chrome"
    if (count of windows) > 0 then
        set t to active tab of front window
        return (URL of t) & (ASCII character 31) & (title of t)
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


def get_chrome_tab():
    """Return the active Chrome tab's (URL, title), either of them None."""
    try:
        result = subprocess.run(
            ["osascript", "-e", CHROME_URL_SCRIPT],
            capture_output=True, text=True, timeout=3
        )
        url, _, title = result.stdout.rstrip("\n").partition("\x1f")
        return common.strip_url_params(url.strip()) or None, title.strip() or None
    except Exception as e:
        log.debug(f"osascript chrome url error: {e}")
        return None, None


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
        url, tab_title = get_chrome_tab()
        window_title = common.chrome_window_title(window_title, tab_title)
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
    mappings[key] = {"project": int(project_id), "activity": int(activity_id), "manual": True}
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


DESCRIPTION_MAX = 500


def _tag(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"({m}m)" if not h else f"({h}h)" if not m else f"({h}h{m}m)"


def _minutes(seconds: list, total_minutes: int) -> list:
    """Whole minutes per entry that add up to total_minutes: each gets its
    floor, and the leftover minutes go to the largest remainders."""
    total = sum(seconds)
    if not total:
        # Nothing to weigh by: the minutes go to the first entry.
        return [total_minutes] + [0] * (len(seconds) - 1)
    exact = [s * total_minutes / total for s in seconds]
    out = [int(x) for x in exact]
    for i in sorted(range(len(exact)), key=lambda i: out[i] - exact[i])[:total_minutes - sum(out)]:
        out[i] += 1
    return out


def describe_group(key: str, group: dict) -> str:
    """The Kimai description: every title that names the work, longest first,
    each with its time, as "Title (25m); Other title (1h5m)". Time under titles
    that name nothing (a new tab, an inbox, a sign-in page) is "other (7m)" at
    the end, so the tags add up to the timesheet's length. Other models read
    these descriptions to judge what the time was for, and daily-briefing
    parses the format, splitting only at a "; " right after a tag."""
    named = defaultdict(float)
    other = 0.0
    for title, secs in (group.get("titles") or {}).items():
        clean = common.clean_title(title)
        if clean:
            named[clean] += secs
        else:
            other += secs
    # Whole minutes, as the Sent log line counts them.
    total_minutes = int(group["seconds"] // 60)
    if not named:
        label = key.split(common.TITLE_SEP, 1)[0].split(":", 1)[-1]
        return f"{label} {_tag(total_minutes)}"
    titles = sorted(named, key=lambda t: -named[t])
    mins = _minutes([named[t] for t in titles] + [other], total_minutes)
    other_min = mins.pop()
    parts = []
    for title, m in zip(titles, mins):
        segment = f"{title} {_tag(m)}"
        # Room for the segment and for an "other" at the end.
        if m == 0 or len("; ".join(parts + [segment])) > DESCRIPTION_MAX - 20:
            other_min += m
            continue
        parts.append(segment)
    if not parts:
        # No title earned a minute of its own: the longest one takes one from
        # "other" when there is one, so the description still names the work.
        first = 1 if other_min else 0
        parts.append(f"{titles[0][:DESCRIPTION_MAX - 20]} {_tag(first)}")
        other_min -= first
    if other_min:
        parts.append(f"other {_tag(other_min)}")
    return "; ".join(parts)


_parse_z = common.parse_z


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


def send_to_kimai(config: dict, date_str: str, entries: list, sources=None) -> dict:
    """Sync external calendar meetings, then classify newly-seen activity and
    send the day's aggregated entries to Kimai (minus any time already covered
    by a synced meeting).

    `sources` selects which halves of the day to send, defaulting to both. They
    are settled independently by the caller, because the Mac log is complete at
    midnight while the phone's day may not exist yet.

    Returns {source: (sent, failed, settled)}. `settled` is False only when the
    source cannot be judged yet, which today means the phone data has not
    arrived; such a source must be retried rather than recorded as done.
    Per-entry errors are caught so one bad group cannot abort the rest, so
    callers must check `failed` before recording a source as sent."""
    sources = set(sources or ALL_SOURCES)
    results = {}

    # Meetings are needed by both halves: the Mac side subtracts them, and the
    # phone side clips against them. calendar_sync skips any uid it has already
    # posted, so calling this on an android-only send cannot duplicate them.
    meetings = _sync_meetings(config, date_str)
    entries = subtract_meetings(entries, meetings)

    mappings = ai_matcher.classify_and_save(config, entries)
    # The Mac side is filed per window title where the key alone cannot say
    # what the work was. Split with the mappings the classifier just saved, so
    # the keys it classified are the keys looked up below.
    entries = common.split_by_title(entries, config, mappings)
    default_project = config.get("default_project_id")
    default_activity = config.get("default_activity_id")

    if SOURCE_MAC in sources:
        # Not caught: if Kimai cannot list its projects it cannot take the
        # timesheets either, and a send that fails is retried, where one that
        # quietly filed Granola under the default would be recorded as done.
        project_customer = ({p["id"]: p.get("customer") for p in common.list_kimai_projects(config)}
                            if common.needs_project_customer(config) else {})

        def resolve_mac(key: str):
            return common.resolve_mapping(key, mappings, default_project, default_activity,
                                          config, project_customer)

        mac_entries = entries
        if config.get("context_keys"):
            mac_entries, extra = common.assign_context(entries, config, resolve_mac,
                                                       project_customer)
            mappings = {**mappings, **extra}

        sent, failed = _post_groups(config, date_str,
                                    aggregate_entries(mac_entries), resolve_mac)
        results[SOURCE_MAC] = (sent, failed, True)

    if SOURCE_ANDROID in sources:
        results[SOURCE_ANDROID] = _send_android(config, date_str, entries,
                                                meetings, mappings)
    return results


def _send_android(config: dict, date_str: str, mac_entries: list, meetings: list,
                  mappings: dict) -> tuple:
    """Returns (sent, failed, settled). See send_to_kimai for `settled`."""
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
        # Nothing to wait for, so the source is settled rather than pending.
        return 0, 0, True
    try:
        import android_sync
    except Exception as e:
        log.error(f"android_sync import failed: {e}")
        return 0, 0, False

    phone = android_sync.sync_day(config, date_str)
    if not phone:
        # sync_day stores the day whenever the fetch succeeded, including a day
        # the phone genuinely was not used, which writes an empty list. So a
        # stored day means "asked and answered" and an absent one means the
        # upload has not arrived: the difference between nothing to send and
        # not knowing yet.
        return 0, 0, has_android_data(date_str)

    before = sum(e["seconds"] for e in phone)
    phone = subtract_intervals(phone, entry_intervals(mac_entries) + list(meetings))
    after = sum(e["seconds"] for e in phone)
    if before != after:
        log.info(f"Android: clipped {(before - after) // 60}m of {before // 60}m "
                 f"that overlapped Mac or meeting time")
    if not phone:
        log.info("Android: everything overlapped Mac time, nothing to send")
        return 0, 0, True

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
    sent, failed = _post_groups(config, date_str, aggregate_entries(phone),
                                resolve_android, tags="android")
    return sent, failed, True


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

# A day's work reaches Kimai from two independent sources, and they do not
# arrive together: the Mac log is complete at midnight, while the phone's day
# only exists once RescueTime has uploaded it, which can be hours later or never.
# Recording the day as one atom meant a day sent before the phone data landed
# could never have it added, so it was lost with nothing saying so.
SOURCE_MAC = "mac"
SOURCE_ANDROID = "android"
ALL_SOURCES = (SOURCE_MAC, SOURCE_ANDROID)

# RescueTime's free plan serves two weeks of history. Past that a day's phone
# time cannot be fetched at all, so retrying it forever is just noise.
PHONE_HISTORY_DAYS = 14


def load_sent_state() -> dict:
    """{date: set(sources)} recording which sources have reached Kimai.

    Reads the legacy format too: a bare list of dates, each meaning the whole
    day went. Those are recorded as both sources sent, so migrating never
    re-sends a historical day.
    """
    if not os.path.exists(SENT_FILE):
        return {}
    with open(SENT_FILE) as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return {d: set(ALL_SOURCES) for d in raw}
    return {d: set(v) for d, v in raw.items()}


def sent_state_is_legacy() -> bool:
    """True while sent_dates.json still holds the bare list of dates."""
    if not os.path.exists(SENT_FILE):
        return False
    try:
        with open(SENT_FILE) as f:
            return isinstance(json.load(f), list)
    except (json.JSONDecodeError, OSError):
        return False


def backup_sent_state() -> str | None:
    """Copy sent_dates.json aside before the format changes under it. This file
    is the only thing preventing a day being sent twice, so it is worth a copy."""
    if not os.path.exists(SENT_FILE):
        return None
    dest = f"{SENT_FILE}.legacy-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(SENT_FILE, dest)
    return dest


def save_sent_state(state: dict):
    """Write atomically: the daemon may be mid-send when this is replaced."""
    tmp = SENT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({d: sorted(v) for d, v in sorted(state.items())}, f, indent=0)
    os.replace(tmp, SENT_FILE)


def mark_source_sent(date_str: str, source: str):
    state = load_sent_state()
    state.setdefault(date_str, set()).add(source)
    save_sent_state(state)


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
    results = send_to_kimai(config, date_str, entries)
    total_sent = sum(r[0] for r in results.values())
    total_failed = sum(r[1] for r in results.values())
    for source, (sent, failed, settled) in sorted(results.items()):
        if settled and not failed:
            mark_source_sent(date_str, source)
        elif not settled:
            print(f"  {source}: no data yet, left outstanding so it retries later.")
    if total_failed:
        print(f"Done — {total_sent} entries sent, {total_failed} failed. "
              f"The failed sources stay outstanding so they retry later.")
    else:
        print(f"Done — {total_sent} entries sent.")


def cmd_sync_android(date_str: str):
    """Pull the phone's day from RescueTime and store it, without sending."""
    import android_sync
    config = common.load_config()
    if not config.get("android_sync_enabled"):
        # sync_day returns stored data without fetching when the flag is off, so
        # without this the command reports an idle phone for a day it never
        # asked about.
        print('android_sync_enabled is false in config.json — nothing was fetched. '
              'Set it to true (and set rescuetime_api_key and rescuetime_timezone) first.')
        return
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
    # Meetings have to be clipped here as well as in the --send path, or phone
    # time overlapping a synced meeting bills twice. _sync_meetings is safe to
    # call again: calendar_sync skips any uid already in synced_meetings.json
    # and still returns the full interval list for the day.
    meetings = _sync_meetings(config, date_str)
    print(f"Sending phone time for {date_str} (clipping against {len(mac)} Mac "
          f"entries and {len(meetings)} meetings already in Kimai)...")
    sent, failed, settled = _send_android(config, date_str, mac, meetings, mappings)
    if not settled:
        print(f"No phone data stored for {date_str} yet — nothing sent. "
              f"RescueTime may not have uploaded it.")
        return
    if not failed:
        mark_source_sent(date_str, SOURCE_ANDROID)
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
        if sent_state_is_legacy():
            backup = backup_sent_state()
            log.info(f"sent_dates.json is in the pre-per-source format; copied to "
                     f"{os.path.basename(backup)} before migrating. Every day in it "
                     f"is recorded as fully sent, so none is revisited.")
            self.sent_state = load_sent_state()
            save_sent_state(self.sent_state)
        else:
            self.sent_state = load_sent_state()

        self._kimai_active_cache: bool = False
        self._kimai_last_check: datetime.datetime = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=KIMAI_CHECK_INTERVAL)
        )

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT, self._on_shutdown)

    def _reload_config(self):
        """Re-read config.json, keeping the previous values if it cannot be read.

        The daemon used to read config once, in __init__, so a value corrected
        while it ran had no effect until the process was restarted. A corrected
        kimai_timezone was therefore ignored for as long as the process lived,
        and every entry written in that time went in against the old zone.
        """
        try:
            self.config = common.load_config()
        except Exception as e:
            log.error(f"Could not re-read config.json, keeping the previous "
                      f"values: {e}")
        return self.config

    def _android_ready(self, date_str: str) -> bool:
        """False while the phone's day is too fresh to be worth fetching.
        RescueTime's free tier uploads on a 30-minute cycle, so asking at 00:00
        spends a call on a day that is not there yet. This gates the phone
        source only: the Mac side never waits on it."""
        if not self.config.get("android_sync_enabled"):
            return False
        try:
            delay = int(self.config.get("android_sync_delay_minutes", 45))
            day = datetime.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            return True
        ready_at = (datetime.datetime.combine(day, datetime.time.min)
                    + datetime.timedelta(days=1, minutes=delay))
        return datetime.datetime.now() >= ready_at

    def _phone_aged_out(self, date_str: str) -> bool:
        """True once a day is older than RescueTime will serve, so its phone
        time can never be fetched and retrying it is noise."""
        try:
            day = datetime.date.fromisoformat(date_str)
        except ValueError:
            return False
        return (datetime.date.today() - day).days > PHONE_HISTORY_DAYS

    def _outstanding(self, date_str: str) -> set:
        """Sources for this day that have not reached Kimai yet."""
        want = {SOURCE_MAC}
        if self.config.get("android_sync_enabled"):
            want.add(SOURCE_ANDROID)
        pending = want - self.sent_state.get(date_str, set())
        if SOURCE_ANDROID in pending and not self._android_ready(date_str):
            pending.discard(SOURCE_ANDROID)
        return pending

    def _record(self, date_str: str, source: str):
        mark_source_sent(date_str, source)
        self.sent_state.setdefault(date_str, set()).add(source)

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
        yesterday = self.current_date.isoformat()
        self._reload_config()
        log.info(f"Midnight rollover — settling {yesterday}")
        self._send_day(yesterday)
        # Always advance the date, even when a source is left outstanding —
        # otherwise the rollover re-fires on every poll. Outstanding sources are
        # retried by _send_missed_days.
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
        """Settle every finished day that still has a source outstanding."""
        self._reload_config()
        common.check_kimai_timezone(self.config)
        today = datetime.date.today().isoformat()
        dates = {f[:-5] for f in os.listdir(LOGS_DIR) if f.endswith(".json")}
        if self.config.get("android_sync_enabled"):
            dates |= android_dates()
        for date_str in sorted(dates):
            if date_str >= today:
                continue  # don't send today's partial log
            self._send_day(date_str)

    def _send_day(self, date_str: str):
        """Send whichever of the day's sources have not reached Kimai yet.

        The two are settled independently, so a day whose Mac time went at
        midnight can still have its phone time added days later without
        re-sending anything.
        """
        pending = self._outstanding(date_str)

        if SOURCE_ANDROID in pending and self._phone_aged_out(date_str):
            log.error(f"{date_str}: the phone data never arrived and the day is "
                      f"now past RescueTime's {PHONE_HISTORY_DAYS}-day history, "
                      f"so it can no longer be fetched. Recording it as done; "
                      f"that day's phone time is lost.")
            self._record(date_str, SOURCE_ANDROID)
            pending.discard(SOURCE_ANDROID)

        if not pending:
            return

        entries = load_entries(os.path.join(LOGS_DIR, f"{date_str}.json"))
        if SOURCE_MAC in pending and not entries:
            # No log file, or an empty one: nothing was captured that day, so
            # there is nothing to send and nothing to keep retrying.
            self._record(date_str, SOURCE_MAC)
            pending.discard(SOURCE_MAC)
            if not pending:
                return

        log.info(f"{date_str}: sending {', '.join(sorted(pending))}")
        try:
            results = send_to_kimai(self.config, date_str, entries, sources=pending)
        except Exception as e:
            log.error(f"Failed to send {date_str}: {e}")
            return
        for source, (sent, failed, settled) in results.items():
            self._settle_source(date_str, source, sent, failed, settled)

    def _settle_source(self, date_str: str, source: str, sent: int, failed: int,
                       settled: bool) -> bool:
        """Decide whether one source of one day counts as done.

        A source is left outstanding when nothing got through, which is the
        network-down case and safe to retry wholesale, or when it cannot be
        judged yet, which is the phone data not having arrived. A partial
        failure is recorded as sent anyway: re-sending would duplicate the
        entries that did land, so the stragglers are logged instead.
        """
        if not settled:
            log.info(f"{date_str}: {source} data has not arrived yet, will retry")
            return False
        if failed and not sent:
            log.warning(f"{date_str}: all {failed} {source} entries failed to "
                        f"send — will retry")
            return False
        if failed:
            log.error(
                f"{date_str}: {source}: {sent} sent but {failed} failed. Recording "
                f"as sent to avoid duplicating the {sent} that landed — add the "
                f"failures manually (see the 'Kimai' errors above)."
            )
        self._record(date_str, source)
        return True


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def cmd_self_test() -> int:
    """Exercise the send-state logic against a temporary state file. No network,
    no Kimai, nothing written outside a temp directory.

    Logging is silenced for the duration: these cases deliberately drive the
    failure paths, and their WARNING and ERROR lines would otherwise land in
    tracker.log looking exactly like real send failures on real dates. Fixture
    dates are in 1999 for the same reason, so nothing here can be mistaken for
    a day that exists.
    """
    import tempfile

    global SENT_FILE
    real_sent_file = SENT_FILE
    logging.disable(logging.CRITICAL)
    results = []

    def check(name, got, want):
        results.append((name, got == want, got, want))

    with tempfile.TemporaryDirectory() as tmp:
        SENT_FILE = os.path.join(tmp, "sent_dates.json")

        # the legacy format migrates to both sources, so nothing is revisited
        with open(SENT_FILE, "w") as f:
            json.dump(["1999-01-01", "1999-01-02"], f)
        check("legacy detected", sent_state_is_legacy(), True)
        state = load_sent_state()
        check("legacy migrates to both sources",
              state["1999-01-01"], {SOURCE_MAC, SOURCE_ANDROID})
        backup_sent_state()
        save_sent_state(state)
        check("after migrating, no longer legacy", sent_state_is_legacy(), False)
        check("round-trips", load_sent_state()["1999-01-02"],
              {SOURCE_MAC, SOURCE_ANDROID})

        # one source at a time
        mark_source_sent("1999-01-03", SOURCE_MAC)
        check("records one source", load_sent_state()["1999-01-03"], {SOURCE_MAC})
        mark_source_sent("1999-01-03", SOURCE_ANDROID)
        check("tops up the other later", load_sent_state()["1999-01-03"],
              {SOURCE_MAC, SOURCE_ANDROID})

        t = object.__new__(Tracker)
        t.sent_state = load_sent_state()

        # what is outstanding, with the phone off and on
        t.config = {"android_sync_enabled": False}
        check("phone off: fresh day wants mac only",
              t._outstanding("1999-01-04"), {SOURCE_MAC})
        check("phone off: settled day wants nothing",
              t._outstanding("1999-01-03"), set())

        t.config = {"android_sync_enabled": True, "android_sync_delay_minutes": 45}
        old_day = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        check("phone on: old day wants both",
              t._outstanding(old_day), {SOURCE_MAC, SOURCE_ANDROID})
        t.sent_state[old_day] = {SOURCE_MAC}
        check("mac already sent leaves the phone outstanding",
              t._outstanding(old_day), {SOURCE_ANDROID})

        # today is still inside the upload delay, so the phone is not asked yet
        today = datetime.date.today().isoformat()
        check("phone not asked before the upload delay",
              t._outstanding(today), {SOURCE_MAC})

        # ageing out
        check("recent day has not aged out", t._phone_aged_out(old_day), False)
        gone = (datetime.date.today()
                - datetime.timedelta(days=PHONE_HISTORY_DAYS + 1)).isoformat()
        check("day past the history window has aged out",
              t._phone_aged_out(gone), True)

        # settling
        t.sent_state = {}
        check("unsettled source is not recorded",
              t._settle_source("1999-01-05", SOURCE_ANDROID, 0, 0, False), False)
        check("  and stays outstanding", "1999-01-05" in load_sent_state(), False)
        check("total failure is not recorded",
              t._settle_source("1999-01-05", SOURCE_MAC, 0, 3, True), False)
        check("partial failure is recorded to avoid duplicates",
              t._settle_source("1999-01-05", SOURCE_MAC, 4, 1, True), True)
        check("clean send is recorded",
              t._settle_source("1999-01-06", SOURCE_MAC, 4, 0, True), True)
        check("  and only that source",
              load_sent_state()["1999-01-06"], {SOURCE_MAC})

    # title-split keys: mixed keys are filed per window title
    cfg = {"default_project_id": 53}
    maps = {"chrome:nytimes.com": {"project": 61}, "app:todoist": {"project": 53}}
    def keyed(e):
        return common.entry_key(common.split_by_title([e], cfg, maps)[0])
    check("Slack splits by workspace title, unread count dropped",
          keyed({"source": "app", "key": "app:slack",
                 "window": "Joe (DM) - Evens Foundation - 2 new items - Slack"}),
          "app:slack | joe (dm) - evens foundation - slack")
    check("an unreadable Chrome URL splits by window title",
          keyed({"source": "chrome", "key": "chrome:internal",
                 "window": "EVENS - Google Chrome - Matt (evensfoundation.eu)"}),
          "chrome:internal | evens - google chrome - matt (evensfoundation.eu)")
    check("a Claude session title loses its spinner",
          keyed({"source": "terminal", "key": "~", "window": "\u2733 OLX buyer message triage"}),
          "~ | olx buyer message triage")
    check("a key pinned to the default project splits too",
          keyed({"source": "app", "key": "app:todoist", "window": "Send WTF 1 pager to board \u2013 Todoist"}),
          "app:todoist | send wtf 1 pager to board \u2013 todoist")
    check("a title that names nothing keeps the base key",
          keyed({"source": "app", "key": "app:slack", "window": "Slack"}), "app:slack")
    check("a shell prompt keeps the base key",
          keyed({"source": "terminal", "key": "~", "window": "-zsh"}), "~")
    check("a key mapped to a real project is not split",
          keyed({"source": "chrome", "key": "chrome:nytimes.com", "window": "Politics - NYT"}),
          "chrome:nytimes.com")
    check("memory warnings and group marks do not make new keys",
          common.normalize_title("EVENS - Part of group \u231bClaude - High memory usage - 861 MB - Google Chrome - Matt (evensfoundation.eu)"),
          common.normalize_title("EVENS - Part of group \u2705Claude - Google Chrome - Matt (evensfoundation.eu)"))
    check("an incognito window is never named",
          common.normalize_title("Tickets | MEO Blueticket - Google Chrome (Incognito)"), "")
    check("a size-only terminal title names nothing",
          common.normalize_title("\u2014 189\u2013544"), "")
    check("a null title key falls back to its base key's project",
          common.resolve_mapping("~/projects/x | lightning mode",
                                 {"~/projects/x": {"project": 30, "activity": 29},
                                  "~/projects/x | lightning mode": {"project": None}}, 53, 53),
          (30, 29))
    check("a base key pinned to the default does not count as a fallback",
          common.resolve_mapping("app:todoist | today", {"app:todoist": {"project": 53}}, 53, 53),
          (53, 53))
    check("a key pinned to the default keeps its own activity",
          common.resolve_mapping("app:x", {"app:x": {"project": 53, "activity": 70}}, 53, 53),
          (53, 70))
    check("  and so do the titles split from it",
          common.resolve_mapping("app:x | some title", {"app:x": {"project": 53, "activity": 70}}, 53, 53),
          (53, 70))
    check("Chrome records the tab inside a named window",
          common.chrome_window_title("EVENS - Google Chrome - Matt (evensfoundation.eu)", "OKRs - Google Docs"),
          "OKRs - Google Docs - EVENS - Google Chrome - Matt (evensfoundation.eu)")
    check("an unnamed window is not doubled",
          common.chrome_window_title("Inbox - stempeck@gmail.com - Gmail - Google Chrome - Matt",
                                     "Inbox - stempeck@gmail.com - Gmail"),
          "Inbox - stempeck@gmail.com - Gmail - Google Chrome - Matt")
    check("Gmail's unread count does not make a new key",
          common.normalize_title("Inbox (12) - stempeck@gmail.com - Gmail - Google Chrome - Matt"),
          common.normalize_title("Inbox (3) - stempeck@gmail.com - Gmail - Google Chrome - Matt"))
    pcfg = {"profile_projects": [{"suffix": "(evensfoundation.eu)", "project": 23, "activity": 22}]}
    check("a title that names nothing falls back to its Chrome profile",
          common.resolve_mapping("chrome:docs.google.com | untitled - google chrome - matt (evensfoundation.eu)",
                                 {"chrome:docs.google.com | untitled - google chrome - matt (evensfoundation.eu)": {"project": None}},
                                 53, 53, pcfg), (23, 22))
    check("the personal profile does not match a work suffix",
          common.resolve_mapping("chrome:docs.google.com | untitled - google chrome - matt", {}, 53, 53, pcfg),
          (53, 53))
    gcfg = {"context_keys": {"app:granola": {"customers": [4, 2], "fallback": {"project": 23, "activity": 22}}}}
    pc = {63: 2, 23: 4, 61: 1}
    gran = {"source": "app", "key": "app:granola", "window": "Granola",
            "start": "1999-01-01T10:00:00Z", "end": "1999-01-01T10:30:00Z", "seconds": 1800}
    near = {"source": "app", "key": "app:ctfg", "window": "x",
            "start": "1999-01-01T10:35:00Z", "end": "1999-01-01T10:40:00Z", "seconds": 300}
    far = {"source": "app", "key": "app:evens", "window": "x",
           "start": "1999-01-01T13:00:00Z", "end": "1999-01-01T13:10:00Z", "seconds": 600}
    news = {"source": "app", "key": "app:news", "window": "x",
            "start": "1999-01-01T10:30:00Z", "end": "1999-01-01T10:31:00Z", "seconds": 60}
    res_ = lambda k: {"app:ctfg": (63, 76), "app:evens": (23, 22), "app:news": (61, 61)}.get(k, (53, 53))
    out, extra = common.assign_context([gran, near, far, news], gcfg, res_, pc)
    check("Granola takes the nearest Evens or CTFG work, not the nearest anything",
          extra.get(common.entry_key(out[0])), {"project": 63, "activity": 76})
    out, extra = common.assign_context([gran, news], gcfg, res_, pc)
    check("Granola with no work that day takes the fallback",
          extra.get(common.entry_key(out[0])), {"project": 23, "activity": 22})
    long_title = common.chrome_window_title("EVENS - Google Chrome - Matt (evensfoundation.eu)",
        "Steering committee phase 2 presentation: notes and actions for the fourth quarter review - Google Docs")
    check("a long tab title keeps its profile",
          common.normalize_title(long_title).endswith("matt (evensfoundation.eu)"), True)
    check("a year in brackets is part of the name",
          common.normalize_title("Budget (2026) - Google Sheets - Google Chrome - Matt")
          == common.normalize_title("Budget (2025) - Google Sheets - Google Chrome - Matt"), False)
    check("a work profile overrules a title mapped to another customer",
          common.resolve_mapping("chrome:x | doc - google chrome - matt (evensfoundation.eu)",
                                 {"chrome:x | doc - google chrome - matt (evensfoundation.eu)": {"project": 63, "activity": 76}},
                                 53, 53, pcfg, {63: 2, 23: 4, 20: 4}), (23, 22))
    check("  but keeps a title mapped inside its own customer",
          common.resolve_mapping("chrome:x | doc - google chrome - matt (evensfoundation.eu)",
                                 {"chrome:x | doc - google chrome - matt (evensfoundation.eu)": {"project": 20, "activity": 19}},
                                 53, 53, pcfg, {63: 2, 23: 4, 20: 4}), (20, 19))
    gran_t = {**gran, "key": "app:granola | weekly leads"}
    out, extra = common.assign_context([gran_t, near], gcfg, res_, pc)
    check("a split Granola title still takes the nearest work",
          extra.get(common.entry_key(out[0])), {"project": 63, "activity": 76})
    check("phone entries are never split",
          keyed({"source": "android", "key": "android:todoist", "window": "android:todoist"}),
          "android:todoist")

    # Multi-project folders and the CTFG rule. Customer 2 is CTFG, 4 is Evens.
    hcfg = {"default_project_id": 53,
            "title_split_keys": ["~/projects/hub"],
            "ctfg_title_rule": {"project": 63, "activity": 76},
            "profile_projects": [{"suffix": "google chrome - civictech.guide", "project": 63, "activity": 76}]}
    hmaps = {"~/projects/hub": {"project": 23, "activity": 22},
             "~/projects/daily-briefing": {"project": 23, "activity": 22}}
    hpc = {63: 2, 38: 2, 23: 4, 20: 4, 2: 1}
    def hub_files(window, maps=hmaps, key="~/projects/hub"):
        e = common.split_by_title([{"source": "terminal", "key": key, "window": window}], hcfg, maps)[0]
        return common.resolve_mapping(common.entry_key(e), maps, 53, 53, hcfg, hpc)
    check("a hub session titled with Daniel files to CTFG",
          hub_files("✳ Weekly newsletter text with Daniel"), (63, 76))
    check("an Evens-titled hub session still files to Evens",
          hub_files("✳ Evens weekly summary Google Doc"), (23, 22))
    nk = "~/projects/hub | weekly newsletter text with daniel"
    check("  a more specific CTFG project the model chose is kept",
          hub_files("✳ Weekly newsletter text with Daniel", {**hmaps, nk: {"project": 38, "activity": 38}}),
          (38, 38))
    check("  an Evens project the model chose is overruled",
          hub_files("✳ Weekly newsletter text with Daniel", {**hmaps, nk: {"project": 20, "activity": 19}}),
          (63, 76))
    check("  and so is a personal one",
          hub_files("✳ Weekly newsletter text with Daniel", {**hmaps, nk: {"project": 2, "activity": 2}}),
          (63, 76))
    check("a CTFG title in a folder that is not split is pulled out of it",
          hub_files("✳ CTFG weekly summary doc", key="~/projects/daily-briefing"), (63, 76))
    check("  while the folder's other titles stay whole",
          common.entry_key(common.split_by_title(
              [{"source": "terminal", "key": "~/projects/daily-briefing", "window": "✳ Evening brief"}],
              hcfg, hmaps)[0]), "~/projects/daily-briefing")
    for word in ("Superbloom Slack", "Simply Secure invoice", "Civic Tech Field Guide taxonomies",
                 "Field Guide newsletter"):
        check(f"  '{word}' counts as CTFG", hub_files("✳ " + word), (63, 76))
    check("an Evens Chrome profile window naming CTFG files to CTFG",
          common.resolve_mapping("chrome:internal | ctfg workboard - google chrome - matt (evensfoundation.eu)",
                                 {}, 53, 53, {**hcfg, "profile_projects": [
                                     {"suffix": "(evensfoundation.eu)", "project": 23, "activity": 22}]}, hpc),
          (63, 76))
    check("a word that only contains a CTFG name is not CTFG",
          hub_files("✳ Danielle's lease renewal"), (23, 22))
    check("the matcher never saves an Evens project for a CTFG title",
          common.enforce_title_rule(nk, 23, hcfg, hpc), 63)
    check("  nor a null",
          common.enforce_title_rule(nk, None, hcfg, hpc), 63)
    check("  but keeps a CTFG one",
          common.enforce_title_rule(nk, 38, hcfg, hpc), 38)
    import ai_matcher
    check("the matcher's answers pass through the rule",
          ai_matcher.apply_title_rule({nk: 23, "~/projects/hub | evening brief": None}, hcfg,
                                      [{"id": 63, "customer": 2}, {"id": 23, "customer": 4}]),
          {nk: 63, "~/projects/hub | evening brief": None})
    check("  and leaves other titles alone",
          common.enforce_title_rule("~/projects/hub | evening brief", 23, hcfg, hpc), 23)
    check("the send path loads customers when only the CTFG rule needs them",
          common.needs_project_customer({"ctfg_title_rule": {"project": 63, "activity": 76}}), True)
    check("  and not when no rule does",
          common.needs_project_customer({"default_project_id": 53}), False)
    check("an overruled answer is saved with the rule's activity",
          common.rule_activity(nk, 63, hcfg), 76)
    check("  but a CTFG project the model chose keeps its own",
          common.rule_activity(nk, 38, hcfg), None)
    check("a saved CTFG project with no activity does not borrow the rule's activity",
          hub_files("✳ Weekly newsletter text with Daniel", {**hmaps, nk: {"project": 38}}), (38, 53))
    cm = {"~/projects/ctfg-curator": {"project": 38, "activity": 37}}
    check("a folder mapped to a specific CTFG project keeps its CTFG-titled windows",
          hub_files("✳ CTFG curator taxonomy", cm, key="~/projects/ctfg-curator"), (38, 37))
    check("  even after the matcher saved the generic CTFG project for the title",
          hub_files("✳ CTFG curator taxonomy",
                    {**cm, "~/projects/ctfg-curator | ctfg curator taxonomy": {"project": 63, "activity": 76}},
                    key="~/projects/ctfg-curator"), (38, 37))
    check("  while a title the model put on a specific CTFG project keeps that",
          hub_files("✳ CTFG curator taxonomy",
                    {"~/projects/ctfg-curator": {"project": 63, "activity": 76},
                     "~/projects/ctfg-curator | ctfg curator taxonomy": {"project": 38, "activity": 37}},
                    key="~/projects/ctfg-curator"), (38, 37))
    check("a mapping set by hand beats the CTFG rule",
          hub_files("✳ Weekly newsletter text with Daniel",
                    {**hmaps, nk: {"project": 23, "activity": 22, "manual": True}}), (23, 22))
    check("  while the same mapping saved by the model does not",
          hub_files("✳ Weekly newsletter text with Daniel",
                    {**hmaps, nk: {"project": 23, "activity": 22}}), (63, 76))
    check("a hand-set mapping beats the Chrome profile too",
          common.resolve_mapping("chrome:internal | ctfg budget with daniel - google chrome - civictech.guide",
                                 {"chrome:internal | ctfg budget with daniel - google chrome - civictech.guide":
                                  {"project": 23, "activity": 22, "manual": True}}, 53, 53,
                                 {**hcfg, "profile_projects": [{"suffix": "google chrome - civictech.guide",
                                                                "project": 63, "activity": 76}]}, hpc), (23, 22))
    check("a hand-set mapping to the default project is kept",
          hub_files("✳ Weekly newsletter text with Daniel",
                    {**hmaps, nk: {"project": 53, "activity": 53, "manual": True}}), (53, 53))
    prop = {"id": 63, "name": "CTFG general bucket"}
    check("accepting the model's proposal in the interactive matcher is not a hand-set mapping",
          ai_matcher.hand_set(prop, prop), False)
    check("  typing another project is", ai_matcher.hand_set({"id": 38, "name": "Newsletter"}, prop), True)
    check("  and so is typing one where the model had none", ai_matcher.hand_set({"id": 38}, None), True)
    check("the hand-set mark survives loading and saving the mappings file",
          common.normalize_mapping_value({"project": 23, "activity": 22, "manual": True}),
          {"project": 23, "activity": 22, "manual": True})
    check("  and stays off mappings that never had it",
          common.normalize_mapping_value({"project": 23, "activity": 22}), {"project": 23, "activity": 22})
    check("two projects with no known customer are not the same customer",
          common.enforce_title_rule(nk, 99, hcfg, {23: 4}), 63)
    g_ctfg = {"source": "app", "key": "app:granola", "window": "Daniel / Matt weekly",
              "start": "1999-01-01T13:00:00Z", "end": "1999-01-01T13:30:00Z", "seconds": 1800}
    g_cfg = {**gcfg, **hcfg}
    g_split = common.split_by_title([g_ctfg, far], g_cfg, {})
    g_out, g_extra = common.assign_context(g_split, g_cfg, res_, pc)
    check("a Granola title naming CTFG skips the nearest-work guess and takes the rule",
          common.resolve_mapping(common.entry_key(g_out[0]), g_extra, 53, 53, g_cfg, hpc), (63, 76))
    check("a CTFG profile window without the parentheses files to CTFG",
          common.resolve_mapping("chrome:internal | board notes - google chrome - civictech.guide",
                                 {"chrome:internal": {"project": 53, "activity": 53}}, 53, 53, hcfg, hpc),
          (63, 76))
    check("a CTFG-titled window on a domain mapped to Evens files to CTFG",
          common.resolve_mapping(common.entry_key(common.split_by_title(
              [{"source": "chrome", "key": "chrome:localhost:4310",
                "window": "CTFG - Google Chrome - civictech.guide"}], hcfg,
              {"chrome:localhost:4310": {"project": 36, "activity": 36}})[0]),
              {"chrome:localhost:4310": {"project": 36, "activity": 36}}, 53, 53, hcfg, {**hpc, 36: 4}),
          (63, 76))

    # Descriptions: every meaningful title, longest first, tags that add up
    ct = common.clean_title
    check("Chrome and profile wrapping come off a title",
          ct("Synthloop launch timeline, November 2026 to May 2027 - Google Chrome - Matt (evensfoundation.eu)"),
          "Synthloop launch timeline, November 2026 to May 2027")
    check("  and so do the tab group, memory warning and window name",
          ct("Campaign Builder - FGF 11-06-26 | Mailchimp - EVENS - Part of group \u2705Claude - High memory usage - 879 MB - Google Chrome - Matt (evensfoundation.eu)"),
          "Campaign Builder - FGF 11-06-26 | Mailchimp")
    check("a Meet title loses its prefix, its repeat and the camera notice",
          ct("Meet \u2013 Weekly EF leads meeting - Meet \u2013 Weekly EF leads meeting - Camera and microphone recording - Google Chrome - Matt (evensfoundation.eu)"),
          "Weekly EF leads meeting")
    check("  also without Google Chrome in it",
          ct("Meet \u2013 Dynamics Simulator launch plan draft with Jonathan - Matt (evensfoundation.eu)"),
          "Dynamics Simulator launch plan draft with Jonathan")
    check("a Gmail title keeps its subject only",
          ct("Fwd: FW: Hackademie - youth literacy programme - matt.stempeck@evensfoundation.eu - Stichting Evens Mail - Google Chrome - Matt (evensfoundation.eu)"),
          "Fwd: FW: Hackademie - youth literacy programme")
    check("a Slack DM is short", ct("Daniel Mackisack (DM) - Superbloom, fka Simply Secure - 2 new items - Slack"),
          "Slack DM: Daniel Mackisack")
    check("  and so is a group DM with an unread mark",
          ct("* joe.\u200belborn, Jonathan (DM) - Evens Foundation Board Space - Slack"), "Slack DM: joe.elborn, Jonathan")
    check("a Slack channel is short", ct("general (Channel) - Evens Foundation - 1 new item - Slack"), "Slack: #general")
    check("a Claude Code session title stays as it is", ct("\u2733 Weekly newsletter text with Daniel"),
          "\u2733 Weekly newsletter text with Daniel")
    check("  and its spinner states read as the same title", ct("\u25d1 Todoist #vibecode tasks"), "\u2733 Todoist #vibecode tasks")
    for generic in ("New Tab", "EVENS", "Inbox (46) - matt.stempeck@evensfoundation.eu - Stichting Evens Mail - Google Chrome - Matt (evensfoundation.eu)",
                    "Log in - Combell nv - Google Chrome - Matt (evensfoundation.eu)", "LinkedIn: Log In or Sign Up - Google Chrome - Matt (civictech.guide)",
                    "Gmail - personal - Google Chrome - Matt", "Find in page\n    EVENS", "\u2733 Claude Code",
                    "meet.google.com is sharing a window.", "personal - Google Chrome - Matt",
                    "To save your password to Google Password Manager, the Password Manager icon in the Location Bar",
                    "Search results - mstempeck@gmail.com - Gmail - personal - Google Chrome - Matt"):
        check(f"  generic: {generic[:40]!r}", ct(generic), "")
    check("a sign-in code never reaches a description",
          ct("Your sign in code for Evens Foundation is 168622 - matt.stempeck@evensfoundation.eu - Stichting Evens Mail - Google Chrome - Matt (evensfoundation.eu)"), "")
    for secret in ("Your verification code is 482913", "123456 is your Google verification code",
                   "Use 482913 to sign in to Evens", "Your code for Evens Foundation: 4821",
                   "Your PIN is 4821", "482913 is your security number", "Your code is 2024",
                   "Code: 1999 - Google Chrome - Matt", "Password reset 48213 - Google Chrome - Matt",
                   "Confirmation number 482913 for login",
                   "Your OTP - Google Chrome - Matt", "Passcode for the board meeting",
                   "2FA backup codes 48213 91822 - Google Chrome - Matt",
                   "Reset link https://example.com/reset?token=abc123def - Google Chrome - Matt"):
        check(f"  nor {secret[:30]!r}", ct(secret), "")
    for work in ("\u2733 Fix sign in page", "Login flow redesign - Google Docs - Google Chrome - Matt",
                 "Access code review for repo - Google Chrome - Matt", "\u2733 Code review for the 2026 budget"):
        check(f"  while a work title is kept: {work[:30]!r}", ct(work) != "", True)
    check("a semicolon without a space cannot split it either", ";" in ct("Retro (5m);actions - Google Chrome - Matt"), False)
    check("an incognito title is never named", ct("Bank - Google Chrome - Matt (Incognito)"), "")
    check("a semicolon inside a title cannot split the description",
          ct("NLnet; Apply for a grant - Google Chrome - Matt"), "NLnet, Apply for a grant")

    dg = lambda titles, secs=None: describe_group("chrome:x", {"seconds": secs or sum(titles.values()), "titles": titles})
    check("every title goes in, longest first",
          dg({"Joe (DM) - Evens Foundation - Slack": 720,
              "Synthloop launch timeline - Google Chrome - Matt (evensfoundation.eu)": 1500}),
          "Synthloop launch timeline (25m); Slack DM: Joe (12m)")
    check("the same title from two windows counts once",
          dg({"Campaign Builder - FGF | Mailchimp - EVENS - Google Chrome - Matt (evensfoundation.eu)": 300,
              "Campaign Builder - FGF | Mailchimp - personal - Google Chrome - Matt": 300}),
          "Campaign Builder - FGF | Mailchimp (10m)")
    check("dropped titles' time goes to a final other",
          dg({"Board notes - Google Chrome - Matt (evensfoundation.eu)": 1200, "New Tab": 420}),
          "Board notes (20m); other (7m)")
    check("hours read as 1h5m", dg({"\u2733 Synthloop board notes": 3900}), "\u2733 Synthloop board notes (1h5m)")
    check("  and a whole hour as 1h", dg({"\u2733 Synthloop board notes": 3600}), "\u2733 Synthloop board notes (1h)")
    def tag_minutes(d):
        return sum(int(h or 0) * 60 + int(m or 0) for h, m in re.findall(r"\((?:(\d+)h)?(?:(\d+)m)?\)(?:; |$)", d))
    mixed = {f"Doc number {i} - Google Chrome - Matt": 100 + 37 * i for i in range(9)}
    check("the tags add up to the timesheet's minutes", tag_minutes(dg(mixed)), sum(mixed.values()) // 60)
    many = {f"A long document title that keeps going for a while, number {i} - Google Chrome - Matt": 600 - i for i in range(30)}
    long_d = dg(many)
    check("a long list stops short of 500 characters", len(long_d) <= 500, True)
    check("  and its tags still add up", tag_minutes(long_d), sum(many.values()) // 60)
    check("  and it ends with other", long_d.rsplit("; ", 1)[1].startswith("other ("), True)
    check("a named title too small for a minute of its own still gets one when there is one",
          dg({"Board notes - Google Chrome - Matt": 20, "New Tab": 580}), "Board notes (1m); other (9m)")
    check("titles with no seconds still leave tags that add up",
          describe_group("chrome:x", {"seconds": 600, "titles": {"Board notes - Google Chrome - Matt": 0}}),
          "Board notes (10m)")
    check("tags count whole minutes the way the log does", dg({"Board notes - Google Chrome - Matt": 1540}),
          "Board notes (25m)")
    check("a group of only generic titles is named by its key",
          describe_group("chrome:mail.google.com", {"seconds": 300, "titles": {"Gmail - Google Chrome - Matt": 300}}),
          "mail.google.com (5m)")
    check("a description never holds a line break",
          "\n" in dg({"Find in page\n    Budget 2027 - Google Chrome - Matt": 300}), False)

    SENT_FILE = real_sent_file
    logging.disable(logging.NOTSET)
    failed = [r for r in results if not r[1]]
    for name, ok, got, want in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"         got {got!r}, wanted {want!r}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


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
        elif sys.argv[1] == "--self-test":
            sys.exit(cmd_self_test())
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
