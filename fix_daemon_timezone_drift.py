#!/usr/bin/env python3
"""
One-off repair: un-shift the entries the daemon wrote while holding a stale zone.

WHAT WENT WRONG
    fix_kimai_timezone.py repaired the data on 2026-08-24 at 16:49, and
    config.json's kimai_timezone was corrected to Europe/Lisbon at the same
    time. The tracker daemon had already started at 16:40. It reads config once,
    in Tracker.__init__, and never re-reads it, so it carried the old
    America/New_York value in memory for its whole run and kept writing entries
    five hours early. The daemon was not restarted until 2026-09-22 18:23.

    Everything it wrote in between is affected: its own desktop entries, the
    phone batches it sent, and the calendar meetings it posted through
    calendar_sync, which runs inside the same send path and takes the same
    config object.

    Measured against logs/<date>.json and logs/android/<date>.json by matching
    description and duration, then reading each entry's begin as Europe/Lisbon:
    the offset is 0h on 2026-08-20, +5h every day from 2026-08-24 through
    2026-09-21, and 0h again from 2026-09-22. Durations are unaffected, only
    placement, which is what moves work onto the wrong day.

THE REPAIR
    Identical to fix_kimai_timezone.py: the stored wall clock IS the New York
    wall clock that was sent, so
        true_utc = stored wall clock read as America/New_York
        new value = true_utc expressed in the Kimai user's zone
    Per-entry conversion via zoneinfo, so a DST edge inside the window would be
    handled as 4 or 6 hours rather than a flat 5.

WHAT IS DELIBERATELY LEFT ALONE
    - every id in a fix-timezone-snapshot-*.json, repaired in August
    - every id in a snapshot this script wrote, so a second run cannot double-shift
    - the Workouts project, written by the daily brief, not by the daemon
    - MOVED_BY_PEER, meetings already corrected in another session
    - phone entries after PHONE_OK_FROM, re-sent from RescueTime after the
      daemon restart and therefore already correct
    - anything outside WINDOW, including nine phone entries on the evening of
      2026-08-23 that could not be reconciled against the stored phone log and
      so are left for a human to judge

SAFETY
    Dry run by default. Each entry is recorded to a snapshot as its PATCH
    succeeds, so an interrupted run leaves a complete rollback record and the
    failures stay retryable.

USAGE
    python3 fix_daemon_timezone_drift.py            # dry run
    python3 fix_daemon_timezone_drift.py --verbose  # list every entry
    python3 fix_daemon_timezone_drift.py --apply    # do it
"""

import argparse
import collections
import datetime
import glob
import json
import os
import sys

import common

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("This script needs Python 3.9+ with zoneinfo.")
    sys.exit(1)

# The zone the daemon still believed in, i.e. the zone the stored wall clock is
# in. Same constant and same conversion fix_kimai_timezone.py uses.
WRONG_TZ = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc

# Daemon started 2026-08-24 16:40 with the stale value and was restarted
# 2026-09-22 18:23. End is exclusive. Verified per day against the local logs.
WINDOW_BEGIN = "2026-08-24T00:00:00"
WINDOW_END = "2026-09-22T00:00:00"

SNAPSHOT_GLOB = "daemon-drift-snapshot-*.json"
PRIOR_REPAIR_GLOB = "fix-timezone-snapshot-*.json"

# Phone entries on or before this date were sent by the stale daemon. Later ones
# were re-fetched from RescueTime after the restart and are already correct.
PHONE_OK_FROM = "2026-09-03"

# Corrected in another session before this script ran.
MOVED_BY_PEER = {4469, 4468, 4518, 4519, 4550, 4585, 4586, 4603, 4602, 4615,
                 4614, 4616, 4640, 4660, 4729, 4728, 4787, 4848}

# Written by the daily brief from Garmin, never by the daemon.
SKIP_PROJECT_PREFIX = "workout"


def _naive(ts: str) -> datetime.datetime:
    """The wall-clock part of a Kimai timestamp, dropping its offset."""
    return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def corrected(entry: dict, config: dict):
    """Return (new_begin, new_end) as Kimai-format wall-clock strings."""
    stored = _naive(entry["begin"])
    true_utc = stored.replace(tzinfo=WRONG_TZ).astimezone(UTC).replace(tzinfo=None)
    end_utc = true_utc + datetime.timedelta(seconds=entry["duration"])
    return (common.kimai_local_time(true_utc, config),
            common.kimai_local_time(end_utc, config))


def ids_in(pattern: str) -> set:
    done = set()
    for path in glob.glob(os.path.join(common.BASE_DIR, pattern)):
        try:
            with open(path) as f:
                done |= {row["id"] for row in json.load(f)}
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            print(f"  warning: could not read snapshot {path}; refusing to run")
            sys.exit(1)
    return done


def fetch_window(config: dict) -> list:
    """Every timesheet in the window, paginated (Kimai caps a page at 500)."""
    out, page = [], 1
    while True:
        batch = common.kimai_request(
            config, "GET",
            f"/timesheets?begin={WINDOW_BEGIN}&end={WINDOW_END}"
            f"&size=500&page={page}&full=true")
        if not batch:
            break
        out += batch
        if len(batch) < 500:
            break
        page += 1
    return out


def classify(entry: dict, skip_ids: set) -> str:
    """Why an entry is or is not shifted. 'shift:<kind>' or 'skip:<reason>'."""
    if entry["id"] in skip_ids:
        return "skip:already repaired in a snapshot"
    if entry["id"] in MOVED_BY_PEER:
        return "skip:already moved in another session"
    project = ((entry.get("project") or {}).get("name") or "").lower()
    if project.startswith(SKIP_PROJECT_PREFIX):
        return "skip:Workouts, written by the daily brief"
    tags = {str(t).lower() for t in (entry.get("tags") or [])}
    if "android" in tags:
        if entry["begin"][:10] >= PHONE_OK_FROM:
            return "skip:phone, re-sent after the restart"
        return "shift:phone"
    if "meeting" in tags:
        return "shift:meeting"
    return "shift:desktop tracker"


def build_plan(entries: list, config: dict, skip_ids: set):
    plan, skipped = [], collections.Counter()
    for e in entries:
        verdict = classify(e, skip_ids)
        if verdict.startswith("skip:"):
            skipped[verdict[5:]] += 1
            continue
        nb, ne = corrected(e, config)
        plan.append({"entry": e, "kind": verdict[6:], "new_begin": nb, "new_end": ne,
                     "old_day": _naive(e["begin"]).date(),
                     "new_day": _naive(nb).date()})
    return plan, skipped


def report(plan: list, skipped: collections.Counter, verbose: bool):
    kinds = collections.Counter(p["kind"] for p in plan)
    secs = collections.Counter()
    for p in plan:
        secs[p["kind"]] += p["entry"]["duration"]
    print(f"\n{len(plan)} entries to shift:")
    for k, n in sorted(kinds.items()):
        print(f"  {k:<26} {n:>4} entries  {secs[k] / 3600:>7.1f}h")
    print(f"  {'TOTAL':<26} {len(plan):>4} entries  "
          f"{sum(secs.values()) / 3600:>7.1f}h")

    crossing = [p for p in plan if p["old_day"] != p["new_day"]]
    print(f"\n{len(crossing)} entries move onto a different day, which is what "
          f"corrupts daily and weekly totals.")

    print("\nsample:")
    for p in plan[:3] + crossing[:3]:
        e = p["entry"]
        flag = "  [changes day]" if p["old_day"] != p["new_day"] else ""
        print(f"  id={e['id']:<5} {e['begin'][:16].replace('T', ' ')} -> "
              f"{p['new_begin'][:16].replace('T', ' ')}  "
              f"{e['duration'] // 60:>4}m  {p['kind']:<15} "
              f"{(e.get('description') or '')[:30]!r}{flag}")

    if verbose:
        print("\nevery entry:")
        for p in sorted(plan, key=lambda x: x["entry"]["begin"]):
            e = p["entry"]
            print(f"  id={e['id']:<5} {e['begin'][:16].replace('T', ' ')} -> "
                  f"{p['new_begin'][:16].replace('T', ' ')}  "
                  f"{e['duration'] // 60:>4}m  {p['kind']:<15} "
                  f"{(e.get('description') or '')[:40]!r}")

    if skipped:
        print("\nleft alone:")
        for reason, n in sorted(skipped.items()):
            print(f"  {n:>4}  {reason}")


def apply_plan(plan: list, config: dict):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_path = os.path.join(common.BASE_DIR, f"daemon-drift-snapshot-{stamp}.json")
    # Recorded as each PATCH succeeds, never up front: the snapshot is both the
    # rollback record and the skip list for a later run, so an entry that failed
    # must not appear in it.
    done_rows = []
    ok = failed = drift = 0
    try:
        for p in plan:
            e = p["entry"]
            try:
                res = common.kimai_request(
                    config, "PATCH", f"/timesheets/{e['id']}",
                    {"begin": p["new_begin"], "end": p["new_end"]})
            except Exception as exc:
                print(f"  FAILED {e['id']}: {exc}")
                failed += 1
                continue
            done_rows.append({"id": e["id"], "begin": e["begin"], "end": e.get("end"),
                              "duration": e["duration"],
                              "description": e.get("description")})
            if res.get("duration") != e["duration"]:
                print(f"  duration changed on {e['id']}: "
                      f"{e['duration']}s -> {res.get('duration')}s")
                drift += 1
            ok += 1
    finally:
        with open(snap_path, "w") as f:
            json.dump(done_rows, f, indent=2)
        print(f"\nSnapshot written: {os.path.basename(snap_path)} "
              f"({len(done_rows)} entries changed)")

    print(f"\n{ok} shifted, {failed} failed, {drift} with a duration change.")
    if failed:
        print(f"The {failed} failures are absent from the snapshot, so "
              f"re-running picks them up.")
    print(f"To undo: re-apply the begin/end values in {os.path.basename(snap_path)}.")


def main(apply: bool, verbose: bool):
    config = common.load_config()
    common.check_kimai_timezone(config)

    skip_ids = ids_in(PRIOR_REPAIR_GLOB) | ids_in(SNAPSHOT_GLOB)
    print(f"window: {WINDOW_BEGIN[:10]} to {WINDOW_END[:10]} (end exclusive)")
    print(f"ids protected by existing snapshots: {len(skip_ids)}")

    entries = fetch_window(config)
    print(f"entries in window: {len(entries)}")

    plan, skipped = build_plan(entries, config, skip_ids)
    report(plan, skipped, verbose)

    if not plan:
        print("\nNothing to do.")
        return
    if not apply:
        print("\nDry run. Nothing written. Re-run with --apply to shift these.")
        return
    apply_plan(plan, config)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--verbose", action="store_true",
                    help="list every entry, not just a sample")
    args = ap.parse_args()
    main(args.apply, args.verbose)
