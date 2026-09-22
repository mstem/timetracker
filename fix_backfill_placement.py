#!/usr/bin/env python3
"""
One-off repair: put the backfill's replacement entries back where they belong.

WHAT WENT WRONG
    backfill_urban_league.py split 28 long terminal entries into per-project
    pieces on 2026-08-17. It read each original's begin back from Kimai, which
    returns a timezone-aware value ("2026-08-13T18:59:00+0100"), and handed it
    straight to common.kimai_local_time. That function documents a *naive UTC*
    argument and opens with .replace(tzinfo=utc), so the offset was discarded
    rather than converted and the Lisbon wall clock was converted a second time
    into the zone config.json then claimed, America/New_York. Minus four hours.

    The --fix-rounding pass in the same run did the same thing to the same
    entries, via .replace(tzinfo=None). Minus four hours again.

    fix_kimai_timezone.py then applied its +5h correction to the result on
    2026-08-24, correctly for every entry the backfill had not touched and
    on top of the double shift for every entry it had.

    Net: the replacements sit eight hours before the work they represent, while
    everything around them is right. Durations were never affected, so no total
    is wrong, only placement, which is what moves work onto the wrong day.

THE REPAIR
    The target is not guessed. Each original's stored begin W is still in the
    backfill snapshot, and W was itself written by the plain wrong-timezone bug,
    so the correct position is the same conversion fix_kimai_timezone applies:
    read W as America/New_York, express it in the Kimai user's zone. The splits
    are then laid out contiguously from there, each keeping its own duration.

    Before proposing anything the script reconstructs where each group must be
    sitting *now* by replaying the two bad conversions and the good one, and
    checks that against live Kimai. A group that is not where the model predicts
    is reported and skipped rather than moved on a guess.

SAFETY
    Dry run by default. Every entry that is actually changed is recorded to
    fix-placement-snapshot-<stamp>.json as it succeeds, so an interrupted run
    still leaves a complete rollback record and the failures stay retryable.

USAGE
    python3 fix_backfill_placement.py              # dry run: show every move
    python3 fix_backfill_placement.py --verbose    # also list each split
    python3 fix_backfill_placement.py --apply      # do it
"""

import argparse
import datetime
import glob
import json
import os
import sys
from collections import defaultdict

import common

try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("This script needs Python 3.9+ with zoneinfo.")
    sys.exit(1)

# The zone config.json wrongly claimed while the backfill ran, i.e. the zone the
# bad conversions targeted. Same constant fix_kimai_timezone.py uses.
WRONG_TZ = ZoneInfo("America/New_York")
UTC = datetime.timezone.utc

BACKFILL_GLOB = "backfill-snapshot-*.json"
SNAPSHOT_GLOB = "fix-placement-snapshot-*.json"

# Only the "iterm" group was split and replaced. The "catch_all" entries were
# reassigned in place, keeping their ids and their times, so they were never
# touched by the buggy write path and are already correct.
SPLIT_GROUP = "iterm"

# A split's begin has to land within this of where the model says, to be matched
# to its original. Kimai rounds to the minute, so this is generous on purpose.
MATCH_TOLERANCE = datetime.timedelta(minutes=3)

# Total duration of a matched group may differ from its original by at most this
# much, from the 60s-per-entry rounding the backfill's own pass removed.
DURATION_SLACK_PER_ENTRY = 90


def _naive(ts: str) -> datetime.datetime:
    """The wall-clock part of a Kimai timestamp, dropping its offset."""
    return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def _as_utc_to_wrong(dt: datetime.datetime) -> datetime.datetime:
    """Replay one pass of the bug: read a wall clock as UTC, write it as
    New York wall clock. zoneinfo keeps the DST edges right, where the gap is
    four hours rather than five."""
    return dt.replace(tzinfo=UTC).astimezone(WRONG_TZ).replace(tzinfo=None)


def _repaired(dt: datetime.datetime, config: dict) -> datetime.datetime:
    """Replay fix_kimai_timezone: read a stored wall clock as New York, express
    it in the Kimai user's zone."""
    true_utc = dt.replace(tzinfo=WRONG_TZ).astimezone(UTC).replace(tzinfo=None)
    return _naive(common.kimai_local_time(true_utc, config))


def newest(pattern: str):
    found = sorted(glob.glob(os.path.join(common.BASE_DIR, pattern)))
    return found[-1] if found else None


def load_originals() -> list:
    """The entries the backfill deleted and replaced, from its own snapshot."""
    path = newest(BACKFILL_GLOB)
    if not path:
        print(f"No {BACKFILL_GLOB} found. Nothing to work from.")
        sys.exit(1)
    print(f"backfill snapshot: {os.path.basename(path)}")
    with open(path) as f:
        snap = json.load(f)
    originals = snap.get(SPLIT_GROUP, [])
    if not originals:
        print(f"Snapshot has no {SPLIT_GROUP!r} group. Nothing to repair.")
        sys.exit(1)
    return sorted(originals, key=lambda t: t["begin"])


def already_fixed_ids() -> set:
    """Ids recorded in a previous run's snapshot, so nothing moves twice."""
    done = set()
    for path in glob.glob(os.path.join(common.BASE_DIR, SNAPSHOT_GLOB)):
        try:
            with open(path) as f:
                done |= {row["id"] for row in json.load(f)}
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            print(f"  warning: could not read snapshot {path}; refusing to run")
            sys.exit(1)
    return done


def fetch_window(config: dict, begin: str, end: str) -> list:
    """Every timesheet in a window, paginated (Kimai caps a page at 500)."""
    out, page = [], 1
    while True:
        batch = common.kimai_request(
            config, "GET",
            f"/timesheets?begin={begin}&end={end}&size=500&page={page}&full=true")
        if not batch:
            break
        out += batch
        if len(batch) < 500:
            break
        page += 1
    return out


def build_plan(originals: list, live: list, config: dict, skip: set):
    """Match each original to the splits that replaced it and work out where
    those splits belong. Returns (plan, problems)."""
    by_desc = defaultdict(list)
    for t in live:
        by_desc[(t.get("description") or "").strip()].append(t)
    for group in by_desc.values():
        group.sort(key=lambda t: t["begin"])

    original_ids = {t["id"] for t in originals}
    plan, problems = [], []

    for o in originals:
        # Match the fallback the backfill itself used when it wrote the splits:
        # an original with no description became splits described "terminal".
        desc = (o.get("description") or "").strip() or "terminal"
        stored = _naive(o["begin"])

        # Where the model says the splits must be sitting now.
        damaged = _as_utc_to_wrong(_as_utc_to_wrong(stored))
        predicted = _repaired(damaged, config)
        # Where they belong.
        target = _repaired(stored, config)

        window_end = predicted + datetime.timedelta(seconds=o["duration"]) + MATCH_TOLERANCE
        found = [t for t in by_desc.get(desc, [])
                 if t["id"] not in original_ids
                 and predicted - MATCH_TOLERANCE <= _naive(t["begin"]) <= window_end]

        if not found:
            problems.append(f"id={o['id']} {desc!r} {stored:%Y-%m-%d %H:%M}: "
                            f"no splits found near the predicted "
                            f"{predicted:%Y-%m-%d %H:%M}. Skipped.")
            continue

        first = _naive(found[0]["begin"])
        if abs((first - predicted).total_seconds()) > MATCH_TOLERANCE.total_seconds():
            problems.append(f"id={o['id']} {desc!r}: first split sits at "
                            f"{first:%Y-%m-%d %H:%M}, model says "
                            f"{predicted:%Y-%m-%d %H:%M}. Skipped.")
            continue

        total = sum(t["duration"] for t in found)
        slack = DURATION_SLACK_PER_ENTRY * len(found)
        if abs(total - o["duration"]) > slack:
            problems.append(f"id={o['id']} {desc!r}: {len(found)} splits total "
                            f"{total}s against an original {o['duration']}s, "
                            f"outside the {slack}s rounding allowance. Skipped.")
            continue

        already = [t["id"] for t in found if t["id"] in skip]
        if already:
            problems.append(f"id={o['id']} {desc!r}: ids {already} are in an "
                            f"earlier snapshot, so this group already moved. Skipped.")
            continue

        moves, cursor = [], target
        for t in found:
            end = cursor + datetime.timedelta(seconds=t["duration"])
            moves.append({
                "entry": t,
                "new_begin": _fmt(cursor),
                "new_end": _fmt(end),
            })
            cursor = end
        plan.append({"original": o, "moves": moves,
                     "from": first, "to": target, "total": total})

    return plan, problems


def _fmt(dt: datetime.datetime) -> str:
    """Kimai's HTML5 wall-clock format. The value is already in the Kimai user's
    zone here, so no further conversion belongs on this path."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def report(plan: list, problems: list, verbose: bool):
    entries = sum(len(p["moves"]) for p in plan)
    crossing = 0
    print(f"\n{len(plan)} group(s), {entries} entries to move.\n")
    for p in plan:
        o = p["original"]
        shift = (p["to"] - p["from"]).total_seconds() / 3600
        moved_day = p["from"].date() != p["to"].date()
        crossing += 1 if moved_day else 0
        flag = "  [changes day]" if moved_day else ""
        print(f"  {(o.get('description') or '')[:34]:<34} "
              f"{p['from']:%Y-%m-%d %H:%M} -> {p['to']:%Y-%m-%d %H:%M}  "
              f"{shift:+.0f}h  {len(p['moves']):>2} splits  "
              f"{p['total'] / 3600:.2f}h{flag}")
        if verbose:
            for m in p["moves"]:
                print(f"       id={m['entry']['id']:<6} "
                      f"{_naive(m['entry']['begin']):%m-%d %H:%M} -> "
                      f"{_naive(m['new_begin']):%m-%d %H:%M}  "
                      f"{m['entry']['duration'] // 60}m")
    print(f"\n{crossing} group(s) move onto a different calendar day.")
    if problems:
        print(f"\n{len(problems)} group(s) skipped:")
        for line in problems:
            print(f"  {line}")


def apply_plan(plan: list, config: dict):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_path = os.path.join(common.BASE_DIR,
                             f"fix-placement-snapshot-{stamp}.json")
    # Recorded as each PATCH succeeds, never up front: the snapshot is both the
    # rollback record and the skip list, so an entry that failed must not appear
    # in it or a later run would refuse to retry it.
    done_rows = []

    ok = failed = drift = 0
    try:
        for p in plan:
            for m in p["moves"]:
                e = m["entry"]
                try:
                    res = common.kimai_request(
                        config, "PATCH", f"/timesheets/{e['id']}",
                        {"begin": m["new_begin"], "end": m["new_end"]})
                except Exception as exc:
                    print(f"  FAILED {e['id']}: {exc}")
                    failed += 1
                    continue
                done_rows.append({"id": e["id"], "begin": e["begin"],
                                  "end": e["end"], "duration": e["duration"],
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

    print(f"\n{ok} moved, {failed} failed, {drift} with a duration change.")
    if failed:
        print(f"The {failed} failures are absent from the snapshot, so "
              f"re-running picks them up.")
    print(f"To undo: re-apply the begin/end values in "
          f"{os.path.basename(snap_path)}.")


def main(apply: bool, verbose: bool):
    config = common.load_config()
    common.check_kimai_timezone(config)

    originals = load_originals()
    skip = already_fixed_ids()
    if skip:
        print(f"Skipping {len(skip)} entry/entries recorded in an earlier run.")

    days = sorted({_naive(o["begin"]).date() for o in originals})
    begin = (days[0] - datetime.timedelta(days=2)).isoformat() + "T00:00:00"
    end = (days[-1] + datetime.timedelta(days=2)).isoformat() + "T00:00:00"
    print(f"originals: {len(originals)} across {days[0]} to {days[-1]}")
    print(f"reading live timesheets {begin[:10]} to {end[:10]}...")
    live = fetch_window(config, begin, end)
    print(f"live entries in window: {len(live)}")

    plan, problems = build_plan(originals, live, config, skip)
    report(plan, problems, verbose)

    if not plan:
        print("\nNothing to do.")
        return
    if not apply:
        print("\nDry run. Nothing written. Re-run with --apply to move these.")
        return
    apply_plan(plan, config)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--verbose", action="store_true",
                    help="list every individual split, not just the groups")
    args = ap.parse_args()
    main(args.apply, args.verbose)
