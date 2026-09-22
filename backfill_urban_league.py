#!/usr/bin/env python3
"""One-shot: move miscategorized entries off the archived Urban League projects.

Two problems land here.

1. Nine cross-cutting activities (Beeper, Slack, Todoist, Granola, news sites)
   were classified into "Urban League / General". They belong to no project, so
   they move to Internal / Misc.

2. All iTerm2 time collapsed into a single `app:iterm2` key, because tracker.py
   only recognised Terminal.app and never asked iTerm2 for its working
   directory. That time is recoverable: Claude Code writes a transcript per
   session carrying the real cwd and a timestamp per message, and the tracker
   logs carry true start/end times per span. Joining the two recovers which
   project each span belonged to.

Attribution comes from the tracker logs, NOT from the Kimai timestamps. Kimai's
begin/end are synthetic: tracker.py starts an entry at the day's first span for
a key and runs it contiguously for the total duration, so an 11-hour entry is
one real start time followed by invented placement. Durations are true, clock
positions are not.

Redistribution is per day, not per entry. Several entries can share a day, and
scaling each one against the whole day's attribution double-counts it. The
script asserts seconds-out equals seconds-in and refuses to write otherwise.

Splitting an aggregate across projects means replacing it, so the original is
deleted only after its replacements are safely created. Every entry touched is
written to a snapshot file first.

Usage:
  python3 backfill_urban_league.py                 # dry run
  python3 backfill_urban_league.py --apply         # write
"""

import argparse
import bisect
import datetime
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

import common

SOURCE_PROJECT_IDS = {28, 35}       # Urban League / General, Urban League / Meetings
MISC_PROJECT_ID = 53                # Internal / Misc
MISC_ACTIVITY_ID = 53

# Every Claude Code session store. Reading only ~/.claude misses two thirds of
# the history; synthloop sessions live entirely in account2 and account3.
CLAUDE_ROOTS = [
    "/Users/home/.claude",
    "/Users/home/.claude-account1",
    "/Users/home/.claude-account2",
    "/Users/home/.claude-account3",
    "/Users/home/.claude3",
]

# Claude Code replaces the window title with a spinner plus its current task.
# A blank title is the same source: an iTerm2 span caught mid-redraw.
SPINNER = re.compile(r"^[✳⠐⠂⠄⠆⠇⠋⠙⠹⠸⠼⠴⠦⠧⠏◐◑◒◓]")

CATCH_ALL_PATTERNS = [
    r"beeper", r"\bslack\b", r"\bgranola\b", r"todoist|^today$",
    r"nytimes|feedly|nosnet", r"google calendar",
]


def parse_ts(ts):
    return datetime.datetime.fromisoformat(
        ts.replace("Z", "+00:00").replace("+0100", "+01:00").replace("+0000", "+00:00")
    )


def load_claude_events():
    """[(timestamp, cwd)] across every Claude Code account, sorted by time."""
    events = []
    for root in CLAUDE_ROOTS:
        for f in glob.glob(f"{root}/projects/*/*.jsonl"):
            for line in open(f, errors="replace"):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                ts, cwd = d.get("timestamp"), d.get("cwd")
                if ts and cwd:
                    try:
                        events.append((parse_ts(ts), cwd))
                    except Exception:
                        pass
    events.sort()
    return events


def load_iterm_spans(since, until):
    """Tracker log spans for app:iterm2, which carry true start/end times."""
    spans = []
    for f in sorted(glob.glob(os.path.join(common.LOGS_DIR, "*.json"))):
        day = os.path.basename(f)[:-5]
        if not (since <= day <= until):
            continue
        try:
            entries = json.load(open(f))
        except Exception:
            continue
        for e in (entries if isinstance(entries, list) else entries.values()):
            if isinstance(e, dict) and e.get("key") == "app:iterm2" and e.get("start"):
                e["_day"] = day
                spans.append(e)
    return spans


def attribute_by_day(spans, events):
    """{day: {project_key: seconds}}, misses collected under '(unattributable)'."""
    times = [e[0] for e in events]

    def counts(st, en):
        i, j = bisect.bisect_left(times, st), bisect.bisect_right(times, en)
        return Counter(c for _, c in events[i:j])

    per_day = defaultdict(lambda: defaultdict(int))
    for s in spans:
        st, en = parse_ts(s["start"]), parse_ts(s["end"])
        c = counts(st, en)
        if not c:
            c = counts(st - datetime.timedelta(minutes=5), en + datetime.timedelta(minutes=5))
        key = common.path_key(c.most_common(1)[0][0]) if c else "(unattributable)"
        per_day[s["_day"]][key] += s["seconds"]
    return per_day


def plan_splits(iterm_entries, per_day):
    """Per day, distribute that day's real Kimai duration across its attribution.

    Returns (plan, seconds_in, seconds_out).
    """
    by_day = defaultdict(list)
    for t in iterm_entries:
        by_day[t["begin"][:10]].append(t)

    plan = []
    for day, entries in sorted(by_day.items()):
        attr = per_day.get(day, {})
        base = sum(attr.values())
        if not base:
            for t in entries:
                plan.append({"entry": t, "splits": [("(unattributable)", int(t["duration"]))]})
            continue

        shares = {k: v / base for k, v in attr.items()}
        for t in entries:
            dur = int(t["duration"])
            alloc = {k: int(dur * sh) for k, sh in shares.items()}
            alloc = {k: v for k, v in alloc.items() if v > 0}
            if not alloc:
                alloc = {max(shares, key=shares.get): dur}
            # Integer truncation loses a few seconds; give them to the largest.
            alloc[max(alloc, key=alloc.get)] += dur - sum(alloc.values())
            plan.append({"entry": t, "splits": sorted(alloc.items(), key=lambda x: -x[1])})

    secs_in = sum(int(t["duration"]) for t in iterm_entries)
    secs_out = sum(s for p in plan for _, s in p["splits"])
    return plan, secs_in, secs_out


def probe_rounding(config):
    """Create a deliberately odd-length entry, read it back, delete it.

    Kimai's rounding rules are not exposed through the API, so the only honest
    check is to send a duration that is not a whole minute and see what comes
    back. Returns (rounding_is_off, stored_seconds).
    """
    begin = datetime.datetime(2020, 1, 6, 3, 0, 0)      # long past, out of the way
    end = begin + datetime.timedelta(seconds=90)
    created = common.kimai_request(config, "POST", "/timesheets", {
        "begin": common.kimai_local_time(begin, config),
        "end": common.kimai_local_time(end, config),
        "project": MISC_PROJECT_ID,
        "activity": MISC_ACTIVITY_ID,
        "description": "rounding probe (auto-deleted)",
        "billable": False,
        "exported": False,
    })
    stored = int(created.get("duration") or 0)
    try:
        common.kimai_request(config, "DELETE", f"/timesheets/{created['id']}")
    except Exception as e:
        print(f"  WARNING: probe entry {created['id']} could not be deleted: {e}",
              file=sys.stderr)
    return stored == 90, stored


def fix_rounding(config, args):
    """Undo the minute Kimai added to every entry the backfill created.

    Kimai rounds each timesheet to a whole minute. The backfill turned 28
    entries into 142, so the rounding fired 142 times instead of 28 and
    invented 8520 seconds. Entries already sit on whole minutes, so removing a
    whole minute cannot trigger rounding again. Entries that are exactly one
    minute are rounding artifacts with nothing left underneath, so they go.
    """
    snaps = sorted(glob.glob(os.path.join(common.BASE_DIR, "backfill-snapshot-*.json")))
    if not snaps:
        print("No backfill snapshot found; cannot identify what to correct.")
        sys.exit(1)
    snap = json.load(open(snaps[-1]))
    print(f"snapshot: {snaps[-1]}")

    orig_secs = sum(int(t["duration"] or 0) for t in snap["iterm"])
    orig_desc = {(t.get("description") or "").strip() or "terminal" for t in snap["iterm"]}
    max_old_id = max(t["id"] for t in snap["iterm"] + snap["catch_all"])

    ok, stored = probe_rounding(config)
    print(f"rounding probe: sent 90s, Kimai stored {stored}s")
    if not ok:
        print("\nABORT: Kimai is still rounding. Turn rounding off under "
              "Settings -> Rounding rules, then re-run. Nothing written.")
        sys.exit(1)
    print("rounding is off\n")

    rows, page = [], 1
    while True:
        b = common.kimai_request(
            config, "GET",
            f"/timesheets?begin={args.since}T00:00:00&end={args.until}T23:59:59"
            f"&full=true&user=all&size=100&page={page}")
        if not b:
            break
        rows.extend(b)
        if len(b) < 100:
            break
        page += 1

    mine = [t for t in rows
            if t["id"] > max_old_id
            and ((t.get("description") or "").strip() or "terminal") in orig_desc]
    now_secs = sum(int(t["duration"] or 0) for t in mine)
    inflation = now_secs - orig_secs

    print(f"replacements found : {len(mine)}  ({now_secs}s = {now_secs/3600:.4f}h)")
    print(f"originals were     : {len(snap['iterm'])}  ({orig_secs}s = {orig_secs/3600:.4f}h)")
    print(f"inflation to remove: {inflation}s = {inflation/3600:.4f}h")

    if inflation != 60 * len(mine):
        print(f"\nABORT: inflation ({inflation}s) is not exactly 60s x {len(mine)} entries. "
              "The set of entries is not what this correction assumes. Nothing written.")
        sys.exit(1)

    shrink = [t for t in mine if int(t["duration"] or 0) > 60]
    drop = [t for t in mine if int(t["duration"] or 0) <= 60]
    projected = now_secs - 60 * len(shrink) - sum(int(t["duration"] or 0) for t in drop)

    print(f"\n  shrink by 60s : {len(shrink)} entries")
    print(f"  delete outright: {len(drop)} entries (exactly one minute, nothing underneath)")
    print(f"\nRECONCILE  projected {projected}s vs target {orig_secs}s")
    if projected != orig_secs:
        print(f"ABORT: projection is off by {projected - orig_secs}s. Nothing written.")
        sys.exit(1)
    print("RECONCILE  ok\n")

    if not args.apply:
        print("Dry run. Nothing written. Re-run with --fix-rounding --apply.")
        return

    patched = deleted = failed = 0
    for t in shrink:
        new_end = parse_ts(t["end"]) - datetime.timedelta(seconds=60)
        try:
            common.kimai_request(config, "PATCH", f"/timesheets/{t['id']}",
                                 {"begin": common.kimai_local_time(
                                      parse_ts(t["begin"]).replace(tzinfo=None), config),
                                  "end": common.kimai_local_time(
                                      new_end.replace(tzinfo=None), config)})
            patched += 1
        except Exception as e:
            failed += 1
            print(f"  patch failed id={t['id']}: {e}", file=sys.stderr)
    for t in drop:
        try:
            common.kimai_request(config, "DELETE", f"/timesheets/{t['id']}")
            deleted += 1
        except Exception as e:
            failed += 1
            print(f"  delete failed id={t['id']}: {e}", file=sys.stderr)

    print(f"\npatched {patched}, deleted {deleted}, failed {failed}")

    # Read back and prove the total actually landed where it should.
    rows, page = [], 1
    while True:
        b = common.kimai_request(
            config, "GET",
            f"/timesheets?begin={args.since}T00:00:00&end={args.until}T23:59:59"
            f"&full=true&user=all&size=100&page={page}")
        if not b:
            break
        rows.extend(b)
        if len(b) < 100:
            break
        page += 1
    after = sum(int(t["duration"] or 0) for t in rows
                if t["id"] > max_old_id
                and ((t.get("description") or "").strip() or "terminal") in orig_desc)
    print(f"VERIFY  replacements now {after}s ({after/3600:.4f}h), target "
          f"{orig_secs}s ({orig_secs/3600:.4f}h)  ->  "
          f"{'MATCH' if after == orig_secs else f'OFF BY {after - orig_secs}s'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--until", default=datetime.date.today().isoformat())
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--fix-rounding", action="store_true",
                    help="undo the minute Kimai added to each backfill-created entry")
    args = ap.parse_args()

    if args.fix_rounding:
        fix_rounding(common.load_config(), args)
        return

    config = common.load_config()
    mappings = json.load(open(os.path.join(common.BASE_DIR, "project_mappings.json")))
    mappings = mappings.get("mappings", mappings)

    rows, page = [], 1
    while True:
        b = common.kimai_request(
            config, "GET",
            f"/timesheets?begin={args.since}T00:00:00&end={args.until}T23:59:59"
            f"&full=true&user=all&size=100&page={page}")
        if not b:
            break
        rows.extend(b)
        if len(b) < 100:
            break
        page += 1

    def pid(t):
        p = t.get("project")
        return p.get("id") if isinstance(p, dict) else p

    stuck = [t for t in rows if pid(t) in SOURCE_PROJECT_IDS]
    iterm, catch_all, keep = [], [], []
    for t in stuck:
        desc = (t.get("description") or "").strip()
        if SPINNER.match(desc) or not desc:
            iterm.append(t)
        elif any(re.search(p, desc, re.I) for p in CATCH_ALL_PATTERNS):
            catch_all.append(t)
        else:
            keep.append(t)

    events = load_claude_events()
    spans = load_iterm_spans(args.since, args.until)
    per_day = attribute_by_day(spans, events)
    plan, secs_in, secs_out = plan_splits(iterm, per_day)

    print(f"Claude Code messages: {len(events)} across {len(CLAUDE_ROOTS)} accounts")
    print(f"iTerm2 spans in logs: {len(spans)}")
    print(f"Kimai entries on Urban League: {len(stuck)}")
    print(f"  {len(iterm)} iTerm2-style, to split by project")
    print(f"  {len(catch_all)} cross-cutting apps, to Misc")
    print(f"  {len(keep)} left alone\n")

    print(f"RECONCILE  in  {secs_in}s ({secs_in/3600:.4f}h)")
    print(f"RECONCILE  out {secs_out}s ({secs_out/3600:.4f}h)")
    if secs_in != secs_out:
        print(f"\nABORT: redistribution differs by {secs_out - secs_in}s. Nothing written.")
        sys.exit(1)
    print("RECONCILE  ok, totals match\n")

    def target(key):
        m = mappings.get(key)
        if m and m.get("project") and m.get("activity"):
            return m["project"], m["activity"]
        return MISC_PROJECT_ID, MISC_ACTIVITY_ID

    totals = defaultdict(int)
    for p in plan:
        for key, secs in p["splits"]:
            totals[key] += secs
    unmapped = sorted({k for k in totals if k != "(unattributable)" and k not in mappings})

    print("iTerm2 time redistributes as:")
    for key, secs in sorted(totals.items(), key=lambda x: -x[1]):
        if secs >= 300:
            mark = "  [no mapping -> Misc]" if key in unmapped else ""
            print(f"  {secs/3600:6.2f}h  {key}{mark}")
    if unmapped:
        print(f"\n{len(unmapped)} key(s) without a mapping land in Misc: {', '.join(unmapped)}")

    ca = sum(int(t["duration"]) for t in catch_all)
    kp = sum(int(t["duration"]) for t in keep)
    print(f"\ncross-cutting apps to Misc: {ca/3600:.2f}h over {len(catch_all)} entries")
    print(f"left on Urban League:       {kp/3600:.2f}h over {len(keep)} entries")

    if not args.apply:
        print("\nDry run. Nothing written. Re-run with --apply.")
        return

    snap = os.path.join(common.BASE_DIR,
                        f"backfill-snapshot-{datetime.datetime.now():%Y%m%d-%H%M%S}.json")
    with open(snap, "w") as f:
        json.dump({"iterm": iterm, "catch_all": catch_all}, f, indent=1)
    print(f"\nsnapshot: {snap}")

    moved = created = deleted = failed = 0

    for t in catch_all:
        try:
            common.kimai_request(config, "PATCH", f"/timesheets/{t['id']}",
                                 {"project": MISC_PROJECT_ID, "activity": MISC_ACTIVITY_ID})
            moved += 1
        except Exception as e:
            failed += 1
            print(f"  patch failed id={t['id']}: {e}", file=sys.stderr)

    for p in plan:
        t = p["entry"]
        cursor = parse_ts(t["begin"])
        made = 0
        for key, secs in p["splits"]:
            proj, act = target(key)
            end = cursor + datetime.timedelta(seconds=secs)
            try:
                common.kimai_request(config, "POST", "/timesheets", {
                    "begin": common.kimai_local_time(cursor, config),
                    "end": common.kimai_local_time(end, config),
                    "project": proj,
                    "activity": act,
                    "description": (t.get("description") or "").strip() or "terminal",
                    "billable": False,
                    "exported": False,
                })
                made += 1
                created += 1
            except Exception as e:
                failed += 1
                print(f"  create failed for {key}: {e}", file=sys.stderr)
            cursor = end
        # Drop the original only once every replacement is safely in place.
        if made == len(p["splits"]):
            try:
                common.kimai_request(config, "DELETE", f"/timesheets/{t['id']}")
                deleted += 1
            except Exception as e:
                failed += 1
                print(f"  delete failed id={t['id']}: {e}", file=sys.stderr)
        else:
            print(f"  kept original id={t['id']}: only {made}/{len(p['splits'])} replacements made",
                  file=sys.stderr)

    print(f"\npatched {moved}, created {created}, deleted {deleted}, failed {failed}")


if __name__ == "__main__":
    main()
