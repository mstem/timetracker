#!/usr/bin/env python3
"""
One-off repair: un-shift Kimai timesheets written with the wrong kimai_timezone.

WHAT WENT WRONG
    Kimai stores timesheet begin/end as naive local wall-clock time and reads it
    back in the *Kimai user's* timezone. From the Clockify->Kimai migration
    (2026-07-14) until 2026-08-24, config.json said kimai_timezone
    "America/New_York" — the value copied from config.example.json — while the
    Kimai user's own timezone was "Europe/Lisbon". So common.kimai_local_time
    converted each UTC timestamp to New York wall clock and Kimai then read that
    same wall clock as Lisbon, storing every entry 5 hours early. No error was
    raised anywhere; the API accepts whatever wall clock it is given.

    Measured damage over 2026-07-14..2026-08-24: 678 entries, of which 120
    (45.55h) were pushed across midnight onto the wrong day, which is what
    corrupts daily and weekly totals. Within a day the shift matters less, since
    a tracker entry's clock position is synthesized anyway (durations are true,
    placement is invented — see backfill_urban_league.py).

    Entries before 2026-07-14 came from the Clockify migration with their own
    timestamps and are NOT affected: measured night-time share is 0.0% there
    versus 25.4% inside the window.

THE REPAIR
    The shift is exactly invertible with no guessing. The stored wall clock IS
    the New York wall clock that was sent, so:
        true_utc = stored_wall_clock read as America/New_York
        new_value = true_utc expressed in Europe/Lisbon
    Verified against local logs: of 81 entries that could be matched to a
    tracker log group by description and duration, 80 reconstruct exactly.
    Per-entry conversion via zoneinfo also gets the DST edges right, where the
    gap between the two zones is 4 or 6 hours rather than 5.

    Durations are never touched, only placement.

SAFETY
    Every touched entry is snapshotted to fix-timezone-snapshot-<stamp>.json
    before anything changes, and ids in any existing snapshot are skipped, so a
    second run cannot double-shift. Dry run by default.

USAGE
    python3 fix_kimai_timezone.py                # dry run: show what would move
    python3 fix_kimai_timezone.py --apply        # do it
"""

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

# The zone config.json wrongly claimed, i.e. the zone the stored wall clock is in.
WRONG_TZ = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc

# The Clockify->Kimai migration landed 2026-07-13; the first shifted entries are
# 2026-07-14. The end is exclusive and covers everything sent before the fix.
WINDOW_BEGIN = "2026-07-14T00:00:00"
WINDOW_END = "2026-08-25T00:00:00"

SNAPSHOT_GLOB = "fix-timezone-snapshot-*.json"


def fetch_window(config: dict) -> list:
    """All timesheets in the affected window, paginated (Kimai caps at 500)."""
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


def already_fixed_ids() -> set:
    """Ids recorded in any previous snapshot — never shift one twice."""
    done = set()
    for path in glob.glob(os.path.join(common.BASE_DIR, SNAPSHOT_GLOB)):
        try:
            with open(path) as f:
                done |= {row["id"] for row in json.load(f)}
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            print(f"  warning: could not read snapshot {path}; refusing to run")
            sys.exit(1)
    return done


def _naive(ts: str) -> datetime.datetime:
    """The wall-clock part of a Kimai timestamp, dropping its offset."""
    return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def corrected(entry: dict, config: dict):
    """Return (new_begin, new_end, old_day, new_day) as Kimai-format strings."""
    stored = _naive(entry["begin"])
    true_utc = stored.replace(tzinfo=WRONG_TZ).astimezone(UTC).replace(tzinfo=None)
    end_utc = true_utc + datetime.timedelta(seconds=entry["duration"])
    new_begin = common.kimai_local_time(true_utc, config)
    new_end = common.kimai_local_time(end_utc, config)
    return new_begin, new_end, stored.date(), _naive(new_begin).date()


def main(apply: bool):
    config = common.load_config()
    common.check_kimai_timezone(config)

    skip = already_fixed_ids()
    if skip:
        print(f"Skipping {len(skip)} entry/entries already recorded in a snapshot.\n")

    entries = [e for e in fetch_window(config) if e["id"] not in skip]
    print(f"{len(entries)} timesheets in {WINDOW_BEGIN[:10]}..{WINDOW_END[:10]}\n")

    plan, day_moves = [], 0
    for e in entries:
        nb, ne, old_day, new_day = corrected(e, config)
        if nb == e["begin"][:19]:
            continue                                    # nothing to change
        plan.append((e, nb, ne, old_day, new_day))
        if old_day != new_day:
            day_moves += 1

    print(f"would move {len(plan)} entries; {day_moves} of them onto a different day")
    moved_hours = sum(e["duration"] for e, _, _, o, n in plan if o != n) / 3600
    print(f"time currently attributed to the wrong day: {moved_hours:.2f}h\n")

    for e, nb, ne, old_day, new_day in sorted(plan, key=lambda r: -r[0]["duration"])[:15]:
        flag = f"  DAY {old_day} -> {new_day}" if old_day != new_day else ""
        print(f"  {e['begin'][:16]} -> {nb[:16]}  {e['duration'] // 60:>4}m  "
              f"{str(e.get('description'))[:38]}{flag}")
    if len(plan) > 15:
        print(f"  ... and {len(plan) - 15} more")

    if not apply:
        print("\nDry run. Re-run with --apply to write these changes.")
        return
    if not plan:
        print("\nNothing to do.")
        return

    stamp = _naive(plan[0][0]["begin"]).strftime("%Y%m%d") + "-" + str(len(plan))
    snap_path = os.path.join(common.BASE_DIR, f"fix-timezone-snapshot-{stamp}.json")

    # The snapshot is the rollback record AND the skip list for later runs, so an
    # entry may only appear in it once its PATCH has actually succeeded. Writing
    # the whole plan up front recorded entries that failed, and already_fixed_ids
    # then skipped them forever, leaving them shifted with no route back.
    done_rows = []

    def write_snapshot():
        with open(snap_path, "w") as f:
            json.dump(done_rows, f, indent=2)

    ok = failed = drift = 0
    try:
        for e, nb, ne, _, _ in plan:
            try:
                res = common.kimai_request(config, "PATCH", f"/timesheets/{e['id']}",
                                           {"begin": nb, "end": ne})
            except Exception as exc:
                print(f"  FAILED {e['id']}: {exc}")
                failed += 1
                continue
            done_rows.append({"id": e["id"], "begin": e["begin"], "end": e["end"],
                              "duration": e["duration"],
                              "description": e.get("description")})
            if res.get("duration") != e["duration"]:
                # Kimai rounds timestamps, so flag any entry whose length moved.
                print(f"  duration changed on {e['id']}: "
                      f"{e['duration']}s -> {res.get('duration')}s")
                drift += 1
            ok += 1
    finally:
        # Even on Ctrl-C or a crash, what was changed has to be recoverable.
        write_snapshot()
        print(f"\nSnapshot written: {snap_path} ({len(done_rows)} entries changed)")

    print(f"\n{ok} updated, {failed} failed, {drift} with a duration change.")
    if failed:
        print(f"The {failed} failures were not recorded in the snapshot, so "
              f"re-running picks them up.")
    print(f"To undo: re-apply the begin/end values in {os.path.basename(snap_path)}.")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv[1:])
