#!/usr/bin/env python3
"""
Re-file Evens timesheets that held CTFG, personal or other non-Evens work under
the current rules: per-title splits of multi-project folders and the CTFG title
rule (common.CTFG_TITLE).

A timesheet is in scope when the tracker sent it (a "Sent [...]" line in
tracker.log), it sits on a project of the Evens customer, and it came from one
of the --keys folders or holds an entry whose title names CTFG work.

Each such timesheet keeps its exact Kimai duration. The duration is divided
across the timesheet's window titles in proportion to their seconds in that
day's log, each title is resolved under the current rules, and the pieces are
summed per target project. A timesheet whose pieces all land where it already
is stays untouched. So Kimai's day totals cannot change, and no calendar
meeting is synced or re-clipped along the way.

  python3 refile_evens.py 2026-09-25 2026-11-08            # plan only, changes nothing in Kimai
  python3 refile_evens.py 2026-09-25 2026-11-08 --apply    # snapshot, delete, post

The plan run asks the model about titles it has not seen and saves its answers
to project_mappings.json, as the tracker's own send would.
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys

import ai_matcher
import common
import tracker

SENT = re.compile(r"Sent \[(\d{4}-\d\d-\d\d)\] '(.*)' \((.*)\) → (\d+)m$")
DEFAULT_KEYS = ["~/projects/hub", "~/projects/daily-briefing"]


def sends(first: str, last: str) -> list:
    out = []
    with open(tracker.LOG_FILE, errors="replace") as f:
        for line in f:
            m = SENT.search(line.rstrip("\n"))
            if m and first <= m.group(1) <= last:
                out.append((m.group(1), m.group(2), m.group(3), int(m.group(4))))
    return out


def timesheets(config: dict, first: str, last: str) -> list:
    end = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
    out = []
    for page in range(1, 100):
        try:
            rows = common.kimai_request(
                config, "GET",
                f"/timesheets?begin={first}T00:00:00&end={end}T00:00:00&size=500&page={page}&full=true") or []
        except Exception:
            # Kimai answers a page past the end with 404.
            break
        out += rows
        if len(rows) < 500:
            break
    return out


def group_entries(day_entries: list, key: str, day_keys: set) -> list:
    """The log entries a sent key was summed from. A title key holds the entries
    with that title; a base key that was split that day (some other send that
    day starts with "<key> | ") holds only its untitled leftovers."""
    base = key.split(common.TITLE_SEP, 1)[0]
    split_that_day = any(k.startswith(base + common.TITLE_SEP) for k in day_keys)
    out = []
    for e in day_entries:
        if common.entry_key(e) != base:
            continue
        title = common.normalize_title(e.get("window", ""))
        if common.TITLE_SEP in key:
            if base + common.TITLE_SEP + title == key:
                out.append(e)
        elif not (split_that_day and title):
            out.append(e)
    return out


def plan(config: dict, first: str, last: str, keys: list, evens_project: int) -> list:
    projects = common.list_kimai_projects(config)
    project_customer = {p["id"]: p.get("customer") for p in projects}
    names = {p["id"]: p["name"] for p in projects}
    evens = project_customer[evens_project]
    keys = {common.normalize_key(k) for k in keys}

    by_day_desc = collections.defaultdict(list)
    for t in timesheets(config, first, last):
        by_day_desc[(t["begin"][:10], t.get("description") or "")].append(t)

    sent = sends(first, last)
    day_keys = collections.defaultdict(set)
    for day, _, key, _ in sent:
        day_keys[day].add(key)

    logs = {}
    out = []
    claimed = set()
    for day, desc, key, mins in sent:
        # Several keys can send the same description on one day ("CTFG - Google
        # Chrome - Matt" from three domains), so the logged minutes pick the
        # timesheet too, and each timesheet answers for one send only.
        hits = [t for t in by_day_desc.get((day, desc), [])
                if t["id"] not in claimed and abs(t["duration"] // 60 - mins) <= 1]
        if not hits:
            continue
        if len({t["duration"] // 60 for t in hits}) < len(hits) and len(hits) > 1:
            # Same description and same length: either one is the right one
            # to re-file, but only if they are on the same project.
            if len({t["project"]["id"] for t in hits}) > 1:
                print(f"  ! {day} '{desc}' {mins}m: {len(hits)} timesheets fit on different projects, skipped")
                continue
        sheet = min(hits, key=lambda t: abs(t["duration"] // 60 - mins))
        claimed.add(sheet["id"])
        if project_customer.get(sheet["project"]["id"]) != evens:
            continue
        if day not in logs:
            logs[day] = json.load(open(os.path.join(common.LOGS_DIR, f"{day}.json")))
        entries = group_entries(logs[day], key, day_keys[day])
        base = key.split(common.TITLE_SEP, 1)[0]
        if not entries:
            continue
        if base not in keys and not any(common.CTFG_TITLE.search(common.normalize_title(e.get("window", "")))
                                        for e in entries):
            continue

        mappings = ai_matcher.classify_and_save(config, entries)
        split = common.split_by_title(entries, config, mappings)

        current = (sheet["project"]["id"], sheet["activity"]["id"])
        whole = base in keys

        def resolve(k):
            # Outside the multi-project folders only the CTFG rule moves time;
            # everything else stays where it was sent.
            if not whole and not common.title_rule(k, config):
                return current
            return common.resolve_mapping(k, mappings, config["default_project_id"],
                                          config.get("default_activity_id"), config, project_customer)

        total = sum(e["seconds"] for e in split) or 1
        pieces = collections.defaultdict(lambda: {"seconds": 0.0, "start": None, "titles": collections.Counter()})
        for e in split:
            target = resolve(common.entry_key(e))
            p = pieces[target]
            p["seconds"] += sheet["duration"] * e["seconds"] / total
            p["titles"][e.get("window", "")] += e["seconds"]
            p["start"] = min(p["start"] or e["start"], e["start"])
        if sum(p["seconds"] for t, p in pieces.items() if t != current) < 30:
            continue
        out.append({"sheet": sheet, "key": key, "day": day, "pieces": [
            {"project": proj, "activity": act, "project_name": names.get(proj, str(proj)),
             "seconds": round(p["seconds"]), "start": p["start"],
             "description": p["titles"].most_common(1)[0][0],
             "titles": {t: round(s / 60, 1) for t, s in p["titles"].most_common()}}
            for (proj, act), p in pieces.items()]})
    return out


def show(moves: list):
    moved = collections.defaultdict(float)
    for m in moves:
        s = m["sheet"]
        print(f"\n{m['day']}  #{s['id']}  {s['duration'] / 60:.1f}m  {s['project']['name']}  "
              f"({m['key']})  '{s.get('description')}'")
        for p in sorted(m["pieces"], key=lambda p: -p["seconds"]):
            print(f"    -> {p['project_name']:28} {p['seconds'] / 60:6.1f}m")
            for t, mins in list(p["titles"].items())[:8]:
                print(f"         {mins:5.1f}m  {t[:90]}")
            if p["project"] != s["project"]["id"]:
                moved[p["project_name"]] += p["seconds"] / 3600
    print("\nHours leaving Evens projects, by destination:")
    for name, h in sorted(moved.items(), key=lambda kv: -kv[1]):
        print(f"  {name:28} {h:.2f}h")


def apply(config: dict, moves: list):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = os.path.join(common.BASE_DIR, f"refile-evens-snapshot-{stamp}.json")
    with open(snapshot, "w") as f:
        json.dump(moves, f, indent=2, default=str)
    print(f"Snapshot of {len(moves)} timesheet(s) and their planned pieces: {snapshot}")
    for m in moves:
        common.kimai_request(config, "DELETE", f"/timesheets/{m['sheet']['id']}")
        tags = ",".join(t if isinstance(t, str) else t.get("name", "") for t in m["sheet"].get("tags") or [])
        for p in m["pieces"]:
            start = common.parse_z(p["start"])
            body = {
                "begin": common.kimai_local_time(start, config),
                "end": common.kimai_local_time(start + datetime.timedelta(seconds=p["seconds"]), config),
                "project": p["project"],
                "activity": p["activity"],
                "description": p["description"],
                "billable": False,
                "exported": False,
            }
            if tags:
                body["tags"] = tags
            common.kimai_request(config, "POST", "/timesheets", body)
        print(f"  #{m['sheet']['id']} {m['day']} re-filed into {len(m['pieces'])} timesheet(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("first")
    ap.add_argument("last")
    ap.add_argument("--keys", nargs="*", default=DEFAULT_KEYS)
    ap.add_argument("--evens-project", type=int, default=23,
                    help="any Evens project id; its customer is the one being cleaned")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    config = common.load_config()
    if not config.get("ctfg_title_rule"):
        sys.exit("ctfg_title_rule is not set in config.json")
    moves = plan(config, args.first, args.last, args.keys, args.evens_project)
    show(moves)
    if args.apply:
        apply(config, moves)
    else:
        print("\nPlan only. Nothing in Kimai changed.")


if __name__ == "__main__":
    sys.exit(main())
