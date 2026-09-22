#!/usr/bin/env python3
"""Send the day's "stopped working" time to exist.io.

The tracker already knows when the keyboard went quiet: the spans in
logs/<date>.json end at the last input before the machine went idle. This
takes the end of the last work span before the evening break and writes it
to an exist.io time-of-day attribute ("Done working" by default).

The evening break, not the last span of the day: an hour back at the laptop
after dinner is normal and should not move the stop time. So the day is cut
at the first gap of at least `exist_dinner_gap_minutes` that starts after
`exist_earliest_stop_hour`, and the span before that gap is the stop.

Usage:
    python3 exist_sync.py --setup          # create the attribute, once
    python3 exist_sync.py --dry-run        # print today's stop, send nothing
    python3 exist_sync.py                  # send today's stop
    python3 exist_sync.py --date 2026-09-20
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

import common

EXIST_API = "https://exist.io/api/2"
# exist.io value type 4 is "Time of day (minutes from midnight as integer)",
# per https://developer.exist.io/reference/object_types/#attribute-value-types.
VALUE_TYPE_TIME_OF_DAY = 4
DEFAULT_LABEL = "Done working"
DEFAULT_GROUP = "productivity"
DEFAULT_DINNER_GAP_MINUTES = 60
DEFAULT_EARLIEST_STOP_HOUR = 15
DEFAULT_PADDING_MINUTES = 5


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def exist_token(config: dict) -> str:
    """The OAuth2 write token, taken from the environment.

    The client and the token belong to daily-briefing, which mints them with
    reauth-exist.py for the Garmin tag writeback and keeps the value in
    ~/.config/daily-briefing/secrets.env as EXIST_WRITE_TOKEN. Reading that
    same variable keeps one client, one authorisation, and one place a
    refreshed token lands, instead of a second copy going stale here.
    """
    token = os.environ.get("EXIST_WRITE_TOKEN", "")
    if not token:
        print("ERROR: EXIST_WRITE_TOKEN not set in the environment.")
        print("Re-mint it against the existing exist.io client with:")
        print("  python3 ~/Projects/daily-briefing/reauth-exist.py")
        sys.exit(1)
    return token


def local_zone(config: dict):
    name = config.get("exist_timezone") or config.get("kimai_timezone") or "UTC"
    return common.ZoneInfo(name)


# ---------------------------------------------------------------------------
# Working out the stop time
# ---------------------------------------------------------------------------

def load_spans(date_str: str, config: dict, zone) -> list:
    """Spans for the day, in local time, with video playback dropped."""
    path = os.path.join(common.LOGS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        entries = json.load(f)

    spans = []
    for e in entries:
        if common.is_video_context(e.get("source"), e.get("app"), e.get("url"), config):
            continue
        start = datetime.datetime.strptime(e["start"], "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.datetime.strptime(e["end"], "%Y-%m-%dT%H:%M:%SZ")
        spans.append({
            "start": start.replace(tzinfo=datetime.timezone.utc).astimezone(zone),
            "end": end.replace(tzinfo=datetime.timezone.utc).astimezone(zone),
            "key": e.get("key", ""),
        })
    spans.sort(key=lambda s: s["start"])
    return spans


def find_stop(spans: list, config: dict):
    """Return (stop_datetime, reason) or (None, reason) if the day is empty."""
    if not spans:
        return None, "no spans logged for that day"

    gap = datetime.timedelta(
        minutes=config.get("exist_dinner_gap_minutes", DEFAULT_DINNER_GAP_MINUTES))
    earliest = config.get("exist_earliest_stop_hour", DEFAULT_EARLIEST_STOP_HOUR)

    for before, after in zip(spans, spans[1:]):
        if before["end"].hour < earliest:
            continue
        if after["start"] - before["end"] >= gap:
            mins = round((after["start"] - before["end"]).total_seconds() / 60)
            return before["end"], f"{mins} min gap after it"

    return spans[-1]["end"], "last activity of the day, no evening break found"


def minutes_from_midnight(when: datetime.datetime, config: dict) -> int:
    padded = when + datetime.timedelta(
        minutes=config.get("exist_padding_minutes", DEFAULT_PADDING_MINUTES))
    # A stop padded past midnight would land on the next day's date, which
    # exist.io stores per-day. Clamp instead of silently rolling over.
    if padded.day != when.day:
        return 24 * 60 - 1
    return padded.hour * 60 + padded.minute


# ---------------------------------------------------------------------------
# exist.io
# ---------------------------------------------------------------------------

# daily-briefing owns the writing half of this exist.io client: exist_write.py
# there holds acquire, update, batching and the API's rules, and its Garmin and
# Kimai writers already lean on it. Importing it by path keeps one writer for
# the account rather than a second copy that drifts. The two projects are
# separate repos on the same machine, hence the path rather than a package.
PEER_WRITER = os.path.expanduser("~/Projects/daily-briefing/exist_write.py")


def peer_writer():
    if not os.path.exists(PEER_WRITER):
        print(f"ERROR: {PEER_WRITER} not found — it does the posting for this account.")
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("exist_write", PEER_WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_attribute(token: str, label: str, group: str):
    """The one call exist_write does not cover, since its writers only ever
    acquire attributes that already exist."""
    req = urllib.request.Request(
        EXIST_API + "/attributes/create/?success_objects=1",
        data=json.dumps([{"label": label, "group": group,
                          "value_type": VALUE_TYPE_TIME_OF_DAY,
                          "manual": False}]).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"exist.io {e.code} on create: {detail}") from None


def setup_attribute(config: dict) -> str:
    """Create the attribute and take ownership. Returns its exist.io name."""
    token = exist_token(config)
    label = config.get("exist_attribute_label", DEFAULT_LABEL)
    group = config.get("exist_attribute_group", DEFAULT_GROUP)

    created = create_attribute(token, label, group)
    failed = created.get("failed") or []
    if failed and failed[0].get("error_code") != "exists":
        print("Create failed:", json.dumps(failed, indent=2))
        sys.exit(1)

    # The name exist.io assigns is not guaranteed to match the label, so read
    # it back rather than guessing it from the label. An attribute that already
    # exists reports its name the same way.
    name = (created.get("success") or failed)[0]["name"]

    # acquire keeps manual=True, so the attribute stays editable by hand in the
    # Exist apps even though this writes it.
    acquired, failures = peer_writer().acquire(token, [name])
    if failures:
        print("Acquire failed:", json.dumps(failures, indent=2))
        sys.exit(1)
    return name


def send_stop(config: dict, name: str, date_str: str, minutes: int):
    written, failures = peer_writer().update(
        exist_token(config), [{"name": name, "date": date_str, "value": minutes}])
    if failures:
        raise RuntimeError(json.dumps(failures))
    return written


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Write the day's stop time to exist.io")
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today")
    ap.add_argument("--dry-run", action="store_true", help="print the stop time, send nothing")
    ap.add_argument("--setup", action="store_true",
                    help="create and acquire the exist.io attribute, then exit")
    args = ap.parse_args()

    config = common.load_config()

    if args.setup:
        name = setup_attribute(config)
        print(f"Created and acquired '{name}'.")
        print(f'Add this to config.json:  "exist_attribute_name": "{name}"')
        return

    zone = local_zone(config)
    date_str = args.date or datetime.datetime.now(zone).date().isoformat()

    spans = load_spans(date_str, config, zone)
    stop, reason = find_stop(spans, config)
    if stop is None:
        print(f"{date_str}: {reason}, nothing sent")
        return

    minutes = minutes_from_midnight(stop, config)
    padding = config.get("exist_padding_minutes", DEFAULT_PADDING_MINUTES)
    stamp = f"{minutes // 60:02d}:{minutes % 60:02d}"
    print(f"{date_str}: stopped {stop.strftime('%H:%M')} ({reason}); "
          f"sending {stamp} (+{padding} min) = {minutes} min from midnight")

    if args.dry_run:
        return

    name = config.get("exist_attribute_name")
    if not name:
        print("ERROR: 'exist_attribute_name' not set in config.json. Run --setup first.")
        sys.exit(1)
    send_stop(config, name, date_str, minutes)
    print(f"Sent to exist.io as '{name}'.")


if __name__ == "__main__":
    main()
