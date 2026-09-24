#!/usr/bin/env python3
"""
Android -> Kimai phone-time sync, via the RescueTime Analytic Data API.

The Mac tracker only sees the Mac. This pulls the phone's app usage from
RescueTime and writes it into logs/android/<date>.json in the same entry shape
tracker.py writes, so the normal send path picks it up.

RescueTime is the source because Android gives nothing better: RescueTime,
Digital Wellbeing and ActivityWatch all read the same UsageStatsManager data,
which is an app name and a duration. There are no window titles and no browser
URLs on Android, so phone attribution is per-app and coarse by construction.
The free "Lite" plan serves this endpoint; its 2-week history cap and 30-minute
upload cycle are both fine for a daily sync.

Routing order for a phone app (see tracker.resolve_android_target):
    1. an explicit per-app mapping in project_mappings.json  (always wins)
    2. the RescueTime category -> Kimai project table in android_categories.json
    3. default_project_id / default_activity_id

One-time setup:
    python3 android_sync.py --setup            # create the missing Life projects, write the category table
    python3 android_sync.py --setup --dry-run  # show what it would create, touch nothing

Day-to-day (tracker.py calls sync_day itself; these are for inspection):
    python3 android_sync.py --day 2026-08-23
    python3 android_sync.py --days 7 --dry-run

Requires in config.json:
    rescuetime_api_key      Data API key, https://www.rescuetime.com/rtx/settings/api/key_management
    rescuetime_timezone     the timezone set in your RescueTime account (NOT kimai_timezone)
    android_sync_enabled    true
"""

import collections
import datetime
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import common

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

log = logging.getLogger(__name__)

ANALYTIC_DATA_URL = "https://www.rescuetime.com/anapi/data"
RESCUETIME_USER_AGENT = common.KIMAI_USER_AGENT

# RescueTime buckets minute-resolution data into fixed five-minute slots; this
# is the most granular view the API offers.
BUCKET_SECONDS = 300

# RescueTime Category -> where that time belongs in Kimai, as
# (categories, customer name, project name). Existing projects are reused
# wherever one fits; only the six Life projects with no existing home are new.
# --setup resolves these to ids and writes android_categories.json, which is
# hand-editable afterwards.
CATEGORY_TARGETS = [
    (["General Social Networking"],                             "Life",      "Social media"),
    (["Instant Message", "Voice Chat"],                         "Life",      "Messaging"),
    (["Maps & Regional", "Travel & Outdoors"],                  "Life",      "Travel"),
    (["Photos"],                                                "Life",      "Photos"),
    (["General Shopping"],                                      "Life",      "Shopping"),
    (["General News & Opinion",
      "General Reference & Learning"],                          "Life",      "Reading"),
    (["Health & Medicine"],                                     "Life",      "HEALTH"),
    (["Browsers"],                                              "Life",      "Web browser"),
    (["Music", "General Entertainment", "Video"],               "Life",      "Relaxation"),
    (["Business & Finance"],                                    "Life",      "Finances"),
    (["Writing"],                                               "No client", "Creative"),
]

# Suffixes RescueTime appends to Android app names. Longest-first: stripping
# " Android" first would turn "WhatsApp Messenger Android" into
# "WhatsApp Messenger" but mangle "Instagram for Android" into "Instagram for".
_NAME_SUFFIXES = (" for Android", " - Android", " Android")
# Prefix RescueTime uses for an app it doesn't recognise, leaving a raw package
# name: "mobile - co.hinge.app". The package is kept whole, since its last
# dotted segment ("app") carries no meaning.
_NAME_PREFIX = "mobile - "


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _require(config: dict):
    """Return (api_key, tzname), or (None, None) with a reason logged."""
    key = config.get("rescuetime_api_key", "")
    if not key or str(key).startswith("<"):
        log.error("rescuetime_api_key is not set; skipping Android sync")
        return None, None
    tzname = config.get("rescuetime_timezone")
    if not tzname or str(tzname).startswith("<"):
        # Deliberately no fallback to kimai_timezone: that mirrors the Kimai
        # user's setting (America/New_York here) and is a different thing from
        # the RescueTime account timezone, so falling back would silently shift
        # every phone entry by hours.
        log.error("rescuetime_timezone is not set; skipping Android sync "
                  "(it must match your RescueTime account timezone, and is NOT kimai_timezone)")
        return None, None
    return key, tzname


def fetch_rows(config: dict, date_str: str) -> list:
    """Return RescueTime's mobile activity rows for one day, as list of dicts
    keyed by the API's own row_headers (never by fixed column position)."""
    key, _ = _require(config)
    if not key:
        return []
    params = {
        "key": key,
        "perspective": "interval",
        "resolution_time": "minute",
        "restrict_kind": "activity",
        "restrict_source_type": "mobile",
        "restrict_begin": date_str,
        "restrict_end": date_str,
        "format": "json",
    }
    url = ANALYTIC_DATA_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": RESCUETIME_USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    headers = payload.get("row_headers") or []
    return [dict(zip(headers, row)) for row in payload.get("rows") or []]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def android_app_name(activity: str) -> str:
    """Reduce a RescueTime activity label to a bare app name.

    Instagram for Android      -> Instagram
    WhatsApp Messenger Android -> WhatsApp Messenger
    Anki - Android             -> Anki
    mobile - co.hinge.app      -> co.hinge.app
    """
    s = (activity or "").strip()
    if s.startswith(_NAME_PREFIX):
        return s[len(_NAME_PREFIX):].strip()
    for suffix in _NAME_SUFFIXES:
        if s.endswith(suffix):
            return s[: -len(suffix)].strip()
    return s


def _bucket_start_utc(date_field: str, tzname: str) -> datetime.datetime:
    """RescueTime timestamps a bucket in the account's own timezone. Return the
    naive-UTC equivalent, matching how tracker.py stores times."""
    local = datetime.datetime.strptime(date_field, "%Y-%m-%dT%H:%M:%S")
    if ZoneInfo is None:
        return local
    try:
        aware = local.replace(tzinfo=ZoneInfo(tzname))
    except Exception:
        log.warning(f"Unknown rescuetime_timezone {tzname!r}; treating timestamps as UTC")
        return local
    return aware.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _stamp(dt: datetime.datetime) -> str:
    """The exact format tracker._parse_z requires (stricter than ISO-8601)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_to_entries(rows: list, config: dict) -> list:
    """Convert RescueTime rows into tracker-shaped log entries."""
    _, tzname = _require(config)
    if not tzname:
        return []

    configured = config.get("min_duration_seconds", {})
    min_seconds = configured.get("android", common.DEFAULT_MIN_DURATION_SECONDS["android"])

    # The minimum applies to each app's total for the day, not to a single row:
    # a ten-minute session arrives as two or more separate 300s rows, so a
    # per-row filter would cut real sessions apart.
    day_totals = collections.Counter()
    for r in rows:
        day_totals[android_app_name(r.get("Activity"))] += int(r.get("Time Spent (seconds)") or 0)

    kept_apps = {app for app, total in day_totals.items() if app and total >= min_seconds}
    dropped = sorted(
        ((app, t) for app, t in day_totals.items() if app and app not in kept_apps),
        key=lambda kv: -kv[1])
    if dropped:
        log.info(f"Android: below the {min_seconds}s daily minimum, skipped "
                 + ", ".join(f"{a} ({t}s)" for a, t in dropped[:8])
                 + (f" and {len(dropped) - 8} more" if len(dropped) > 8 else ""))

    # Lay each bucket's apps end to end from the bucket start. Placement is
    # approximate either way (the sender synthesizes final clock positions), but
    # it has to be laid out rather than stacked because the overlap clipping in
    # tracker.py reads start/end. A bucket totalling over 300s spills into the
    # next bucket's window and self-corrects, since that bucket starts from its
    # own boundary.
    by_bucket = collections.defaultdict(list)
    for r in rows:
        by_bucket[r.get("Date")].append(r)

    entries = []
    for date_field in sorted(by_bucket):
        cursor = _bucket_start_utc(date_field, tzname)
        for r in sorted(by_bucket[date_field],
                        key=lambda x: -int(x.get("Time Spent (seconds)") or 0)):
            seconds = int(r.get("Time Spent (seconds)") or 0)
            app = android_app_name(r.get("Activity"))
            if seconds <= 0 or app not in kept_apps:
                continue
            if common.is_blocked("android", app, None, config):
                continue
            start, end = cursor, cursor + datetime.timedelta(seconds=seconds)
            cursor = end
            entries.append({
                "source": "android",
                "app": app,
                "window": app,          # the only description candidate Android offers
                "url": None,
                "key": common.activity_key("android", app, app=app),
                "category": r.get("Category") or "",
                "start": _stamp(start),
                "end": _stamp(end),
                "seconds": seconds,
            })
    return entries


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def android_log_path(date_str: str) -> str:
    return os.path.join(common.ANDROID_LOGS_DIR, f"{date_str}.json")


def load_day(date_str: str) -> list:
    path = android_log_path(date_str)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Android log {path} unreadable ({e}); treating as empty")
        return []


def write_day(date_str: str, entries: list):
    """Write atomically into logs/android/, a directory of its own: the tracker
    daemon does an unlocked read-modify-write of logs/<date>.json every 2s, so
    sharing that file would silently truncate Mac spans."""
    os.makedirs(common.ANDROID_LOGS_DIR, mode=0o700, exist_ok=True)
    path = android_log_path(date_str)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, path)


def sync_day(config: dict, date_str: str) -> list:
    """Fetch, convert and store one day of phone activity. Never raises: a
    RescueTime outage must not block a Kimai send (same contract as
    ai_matcher.classify_and_save)."""
    if not config.get("android_sync_enabled"):
        return load_day(date_str)
    try:
        rows = fetch_rows(config, date_str)
        if not rows:
            # Store the empty answer. A missing file means "never successfully
            # asked", which is what the tracker's phone source waits on; a day
            # the phone genuinely wasn't used must settle rather than be
            # retried until it ages out of RescueTime's history.
            log.info(f"Android: no RescueTime mobile rows for {date_str}")
            write_day(date_str, [])
            return []
        entries = rows_to_entries(rows, config)
        write_day(date_str, entries)
        total = sum(e["seconds"] for e in entries)
        log.info(f"Android: {date_str} -> {len(entries)} entries, "
                 f"{total // 60}m across {len({e['key'] for e in entries})} apps")
        return entries
    except Exception as e:
        log.error(f"Android sync failed for {date_str}: {e}")
        return load_day(date_str)


# ---------------------------------------------------------------------------
# Category -> project table
# ---------------------------------------------------------------------------

def load_categories() -> dict:
    """Return {category: {"project": id, "activity": id}} from
    android_categories.json, or {} when --setup hasn't run."""
    path = common.ANDROID_CATEGORIES_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"{path} unreadable ({e}); phone time will fall back to the default project")
        return {}
    return {k: common.normalize_mapping_value(v) for k, v in raw.items()}


def save_categories(table: dict):
    path = common.ANDROID_CATEGORIES_PATH
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(table, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def cmd_setup(dry_run: bool = False):
    """Create the Kimai projects the category table needs, then write it."""
    import ai_matcher

    config = common.load_config()
    customers = common.list_kimai_customers(config)
    projects = common.list_kimai_projects(config)

    cust_by_name = {c["name"].strip().lower(): c for c in customers}
    proj_by_pair = {
        (ai_matcher._customer_name(p).strip().lower(), p["name"].strip().lower()): p
        for p in projects
    }

    created = []
    table = {}
    for categories, customer_name, project_name in CATEGORY_TARGETS:
        pair = (customer_name.strip().lower(), project_name.strip().lower())
        proj = proj_by_pair.get(pair)
        if proj is None:
            cust = cust_by_name.get(customer_name.strip().lower())
            if cust is None:
                print(f"  SKIP     {customer_name} / {project_name}: no such customer in Kimai")
                continue
            created.append(f"{customer_name} / {project_name}")
            if dry_run:
                print(f"  CREATE   {customer_name} / {project_name}  (+ General activity)")
            else:
                proj = common.create_kimai_project(config, project_name, cust["id"])
                common.create_kimai_activity(config, "General", proj["id"])
                print(f"  CREATED  {customer_name} / {project_name}  -> project {proj['id']}")
                projects.append(proj)
                proj_by_pair[pair] = proj
        else:
            print(f"  REUSE    {customer_name} / {project_name}  -> project {proj['id']}")
        for cat in categories:
            table[cat] = {"project": proj["id"] if proj else None, "activity": None}

    if dry_run:
        print(f"\nWould create {len(created)} project(s): {', '.join(created) or 'none'}")
        print(f"Would write {len(table)} category rows to {common.ANDROID_CATEGORIES_PATH}")
        for cat, t in sorted(table.items()):
            where = f"project {t['project']}" if t["project"] else "the project it would create"
            print(f"  {cat:<38} -> {where}")
        return

    # Resolve one activity per project now, so the send path needs no lookups.
    activities = common.list_kimai_activities(config)
    for pid in {v["project"] for v in table.values() if v["project"]}:
        activities += common.list_kimai_activities(config, pid)
    resolve = ai_matcher.build_activity_resolver(config, activities)
    for cat, target in table.items():
        target["activity"] = resolve(target["project"])

    save_categories(table)
    print(f"\nWrote {len(table)} category rows to {common.ANDROID_CATEGORIES_PATH}")
    for cat, t in sorted(table.items()):
        print(f"  {cat:<38} -> project {t['project']}, activity {t['activity']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_day(date_str: str, dry_run: bool):
    config = common.load_config()
    rows = fetch_rows(config, date_str)
    entries = rows_to_entries(rows, config)
    if not entries:
        print(f"No Android activity for {date_str}.")
        return
    totals = collections.Counter()
    cats = collections.Counter()
    for e in entries:
        totals[e["key"]] += e["seconds"]
        cats[e["category"]] += e["seconds"]
    print(f"{date_str}: {len(entries)} entries, "
          f"{sum(totals.values()) / 3600:.2f}h across {len(totals)} apps\n")
    print("  by app:")
    for k, s in totals.most_common():
        print(f"    {s / 60:7.1f}m  {k}")
    print("\n  by RescueTime category:")
    categories = load_categories()
    for c, s in cats.most_common():
        target = categories.get(c)
        where = f"project {target['project']}" if target else "default project"
        print(f"    {s / 60:7.1f}m  {c:<38} -> {where}")
    print(f"\n  first entry: {entries[0]['start']} .. {entries[0]['end']} "
          f"({entries[0]['app']}, {entries[0]['seconds']}s)")
    if dry_run:
        print(f"\n(dry run: nothing written to {android_log_path(date_str)})")
    else:
        write_day(date_str, entries)
        print(f"\nWrote {android_log_path(date_str)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    dry = "--dry-run" in args
    if "--setup" in args:
        cmd_setup(dry_run=dry)
    elif "--day" in args:
        idx = args.index("--day")
        date = args[idx + 1] if idx + 1 < len(args) else datetime.date.today().isoformat()
        cmd_day(date, dry)
    elif "--days" in args:
        idx = args.index("--days")
        n = int(args[idx + 1]) if idx + 1 < len(args) else 7
        today = datetime.date.today()
        for i in range(n, 0, -1):
            cmd_day((today - datetime.timedelta(days=i)).isoformat(), dry)
            print()
    else:
        print(__doc__)
